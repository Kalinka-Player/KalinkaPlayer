"""Apply the settings Raspberry Pi Imager left on the boot partition, then remove them.

kalinka-card-settings.service runs this at a start that finds Imager's files,
before NetworkManager, avahi, Bluetooth and sshd start. It reads custom.toml,
or failing that the calls Imager writes into firstrun.sh, which it never runs.
Each setting stands alone: one that is not valid is named in the journal and
left out, and the rest still apply. Every input file is then overwritten and
removed, and Imager's systemd.run tokens are taken out of cmdline.txt, whatever
applied. No password, Wi-Fi key, hash or SSH key reaches the journal or a
command line.

Exit status 1 when a file could not be read or something on the card was not
applied.
"""

import argparse
import base64
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("card-settings")

BOOT_DIRS = ("boot/firmware", "boot/efi")
CUSTOM_TOML = "custom.toml"
FIRSTRUN = "firstrun.sh"
CLOUD_INIT = ("user-data", "network-config")
INPUTS = (CUSTOM_TOML, FIRSTRUN, *CLOUD_INIT)
MAX_INPUT_BYTES = 256 * 1024

IMAGER_CUSTOM = "/usr/lib/raspberrypi-sys-mods/imager_custom"
USERCONF = "/usr/lib/userconf-pi/userconf"
FIRSTRUN_COMMANDS = frozenset(
    ("set_hostname", "set_wlan", "enable_ssh", "set_timezone")
)

PROFILE = "etc/NetworkManager/system-connections/kalinka-card.nmconnection"
# The supervisor's nm backend keeps the country it joins with in the same file.
REGULATORY_CONF = "etc/modprobe.d/kalinka-regdom.conf"
NETWORK_MARKER = "run/kalinka-image/card-network"
RFKILL_STATE = "var/lib/systemd/rfkill"
ZONEINFO = "usr/share/zoneinfo"

HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
COUNTRY = re.compile(r"[A-Z]{2}")
HEX_KEY = re.compile(r"[0-9A-Fa-f]{64}")
USER_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
PASSWORD_HASH = re.compile(r"\$[0-9a-zA-Z]+\$[A-Za-z0-9./$=,+-]+")
ZONE = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")
LOGGABLE_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
KEY_TYPES = frozenset(
    (
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    )
)
MAX_KEYS = 16
MAX_KEY_BYTES = 4096
MAX_HASH = 256
MAX_PASSWORD = 128
MAX_ZONE = 64
# All commands together stay inside the unit's 2 min, whose kill skips the cleanup.
COMMAND_TIMEOUT = 15

RESERVED_USERS = frozenset(("root", "kalusr"))
FIRST_UID, LAST_UID = 1000, 60000
# The control page's login: ssh-setup.sh accepts one only in exactly this shape.
ADMIN = "kalinka-admin"
ADMIN_COMMENT = "Kalinka dashboard administrator"

_TOKEN = (
    rb"(?:systemd\.run(?:_success_action|_failure_action)?=\S*"
    rb"|systemd\.unit=kernel-command-line\.target)"
)
_TOKEN_AFTER_SPACE = re.compile(rb"[ \t]+" + _TOKEN + rb"(?=\s|$)")
_LEADING_TOKEN = re.compile(rb"^" + _TOKEN + rb"(?:[ \t]+|(?=\s|$))")

TOML_SETTINGS = {
    ("system", "hostname"): "hostname",
    ("user", "name"): "user",
    ("user", "password"): "password",
    ("user", "password_encrypted"): "password_encrypted",
    ("ssh", "enabled"): "ssh",
    ("ssh", "password_authentication"): "password_authentication",
    ("ssh", "authorized_keys"): "keys",
    ("wlan", "ssid"): "ssid",
    ("wlan", "password"): "wifi_key",
    ("wlan", "password_encrypted"): "wifi_key_encrypted",
    ("wlan", "hidden"): "hidden",
    ("wlan", "country"): "country",
    ("locale", "timezone"): "timezone",
}
TOML_NOT_APPLIED = {
    ("locale", "keymap"): "the keyboard layout",
    ("ssh", "ssh_import_id"): "importing SSH keys from the network",
}
TOML_SECTIONS = frozenset(section for section, _ in [*TOML_SETTINGS, *TOML_NOT_APPLIED])


@dataclass(frozen=True)
class Passwd:
    """One account of /etc/passwd."""

    name: str
    uid: int
    gid: int
    home: str


@dataclass
class Wifi:
    """A network to join, with a passphrase, a 64-digit key, or none for an open one."""

    ssid: str
    key: str = field(repr=False)
    hidden: bool


@dataclass
class Login:
    """An account to create or update, with the card's password, keys or both."""

    name: str
    password: Optional[str] = field(default=None, repr=False)
    hashed: bool = False
    keys: list[str] = field(default_factory=list, repr=False)
    password_authentication: Optional[bool] = None


@dataclass
class Settings:
    """What the card asked for that passed every check."""

    hostname: Optional[str] = None
    country: Optional[str] = None
    wifi: Optional[Wifi] = None
    login: Optional[Login] = None
    ssh: bool = False
    timezone: Optional[str] = None


class System:
    """The running machine, for what files cannot do; tests use a recorder instead."""

    def run(self, argv: list[str], stdin: Optional[bytes] = None) -> bool:
        """Runs argv, true on success; one handed a secret on stdin is kept quiet."""
        quiet = subprocess.DEVNULL if stdin is not None else None
        try:
            done = subprocess.run(
                argv,
                input=stdin,
                stdout=quiet,
                stderr=quiet,
                check=False,
                timeout=COMMAND_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    def chown(self, path: Path, uid: int, gid: int) -> None:
        os.chown(path, uid, gid, follow_symlinks=False)

    def set_hostname(self, name: str) -> None:
        socket.sethostname(name)


class Refusals(logging.Handler):
    """Counts the run's warnings, each something the card asked for and did not get."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


class JournalFormatter(logging.Formatter):
    """Gives each line the syslog priority journald reads off a service's output."""

    def format(self, record: logging.LogRecord) -> str:
        priority = 4 if record.levelno >= logging.WARNING else 6
        return f"<{priority}>{super().format(record)}"


def loggable(name: object) -> str:
    """A name from the card fit for the journal, or a stand-in when it could be any."""
    if isinstance(name, str) and LOGGABLE_NAME.fullmatch(name):
        return name
    return "an unnamed entry"


def read_text(path: Path) -> Optional[str]:
    """The file as text without a BOM, or None, logged, when it cannot be."""
    try:
        with path.open("rb") as file:
            data = file.read(MAX_INPUT_BYTES + 1)
    except OSError as error:
        log.warning(
            "%s cannot be read (%s); nothing in it is applied",
            path.name,
            error.strerror,
        )
        return None
    if len(data) > MAX_INPUT_BYTES:
        log.warning(
            "%s is too large to be Imager's; nothing in it is applied", path.name
        )
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.warning("%s is not UTF-8 text; nothing in it is applied", path.name)
        return None


def toml_position(error: tomllib.TOMLDecodeError) -> str:
    """Where custom.toml broke, from tomllib's message, which may quote the file."""
    found = re.search(r"\(at line (\d+), column \d+\)", str(error))
    return f"line {found.group(1)}" if found else "at its end"


def read_custom_toml(text: str) -> Optional[dict[str, Any]]:
    """custom.toml's settings, unchecked, as raspberrypi-sys-mods' init_config reads."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        log.warning(
            "custom.toml is not valid TOML (%s); nothing in it is applied",
            toml_position(error),
        )
        return None
    version = data.pop("config_version", 1)
    if type(version) is not int or version != 1:
        log.warning(
            "custom.toml has a config_version this image does not read; "
            "nothing in it is applied"
        )
        return None
    values: dict[str, Any] = {}
    for section, table in data.items():
        if section not in TOML_SECTIONS:
            log.info("custom.toml: %s is not applied", loggable(section))
            continue
        if not isinstance(table, dict):
            log.warning("custom.toml: [%s] is not a table, so it is not read", section)
            continue
        for key, value in table.items():
            entry = (section, key)
            if entry in TOML_SETTINGS:
                values[TOML_SETTINGS[entry]] = value
            elif entry in TOML_NOT_APPLIED:
                log.info("custom.toml: %s is not applied", TOML_NOT_APPLIED[entry])
            else:
                log.info("custom.toml: %s.%s is not applied", section, loggable(key))
    return values


def read_firstrun(text: str) -> dict[str, Any]:
    """The settings in the calls Imager writes into firstrun.sh, unchecked and unrun."""
    values: dict[str, Any] = {}
    calls = 0
    for number, line in enumerate(text.splitlines(), 1):
        command = line.strip()
        if not command.startswith((IMAGER_CUSTOM, USERCONF)):
            continue
        calls += 1
        try:
            words = shlex.split(command)
        except ValueError:
            log.warning(
                "firstrun.sh line %d cannot be read, so what it sets is not applied",
                number,
            )
            continue
        if words[0] == USERCONF:
            read_userconf(words[1:], number, values)
        elif words[0] == IMAGER_CUSTOM and len(words) > 1:
            read_imager_custom(words[1], words[2:], number, values)
    if not calls:
        log.warning("firstrun.sh holds none of the settings this image reads")
    return values


def read_userconf(args: list[str], number: int, values: dict[str, Any]) -> None:
    if len(args) != 2:
        log.warning(
            "firstrun.sh line %d: userconf has arguments this image does not read",
            number,
        )
        return
    values.update(user=args[0], password=args[1], password_encrypted=True)


def read_imager_custom(
    command: str, args: list[str], number: int, values: dict[str, Any]
) -> None:
    flags = set()
    while command == "set_wlan" and args and args[0] in ("-h", "-p"):
        flags.add(args.pop(0))
    if command == "set_hostname" and len(args) == 1:
        values["hostname"] = args[0]
    elif command == "set_wlan" and len(args) in (2, 3):
        values.update(ssid=args[0], wifi_key=args[1], hidden="-h" in flags)
        if len(args) == 3:
            values["country"] = args[2]
    elif command == "enable_ssh" and not args:
        values.update(ssh=True, password_authentication=True)
    elif command == "enable_ssh" and args[0] == "-k":
        values.update(ssh=True, password_authentication=False, keys=args[1:])
    elif command == "set_timezone" and len(args) == 1:
        values["timezone"] = args[0]
    elif command == "set_keymap":
        log.info("firstrun.sh: the keyboard layout is not applied")
    elif command in FIRSTRUN_COMMANDS:
        log.warning(
            "firstrun.sh line %d: %s has arguments this image does not read",
            number,
            command,
        )
    else:
        log.info(
            "firstrun.sh line %d: imager_custom %s is not applied",
            number,
            loggable(command),
        )


def read_accounts(root: Path) -> dict[str, Passwd]:
    accounts = {}
    try:
        lines = (root / "etc/passwd").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return accounts
    for line in lines:
        fields = line.split(":")
        if len(fields) == 7 and fields[2].isdigit() and fields[3].isdigit():
            name, uid, gid, home = fields[0], int(fields[2]), int(fields[3]), fields[5]
            accounts[name] = Passwd(name, uid, gid, home)
    return accounts


def password_locked(root: Path, name: str) -> bool:
    """Whether name's password is locked by '!', as useradd and rpi-image-gen do."""
    try:
        lines = (root / "etc/shadow").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        fields = line.split(":")
        if len(fields) > 1 and fields[0] == name:
            return fields[1].startswith("!")
    return False


def valid_hostname(name: Any) -> bool:
    return isinstance(name, str) and bool(HOSTNAME.fullmatch(name))


def valid_country(code: Any) -> bool:
    return isinstance(code, str) and bool(COUNTRY.fullmatch(code))


def valid_ssid(ssid: Any) -> bool:
    if not isinstance(ssid, str):
        return False
    return ssid.isprintable() and 1 <= len(ssid.encode()) <= 32


def valid_wifi_key(key: Any, encrypted: Any) -> bool:
    """A 64-digit key, a WPA passphrase of 8 to 63 printable ASCII characters, or ''."""
    if not isinstance(key, str) or not isinstance(encrypted, (bool, type(None))):
        return False
    hexadecimal = bool(HEX_KEY.fullmatch(key))
    passphrase = 8 <= len(key) <= 63 and all(" " <= char <= "~" for char in key)
    if encrypted is True:
        return hexadecimal
    if encrypted is False:
        return passphrase or not key
    return hexadecimal or passphrase or not key


def valid_user(name: Any, accounts: dict[str, Passwd]) -> bool:
    if not isinstance(name, str) or not USER_NAME.fullmatch(name):
        return False
    if name in RESERVED_USERS:
        return False
    account = accounts.get(name)
    return account is None or FIRST_UID <= account.uid <= LAST_UID


def valid_public_key(key: str) -> bool:
    """An OpenSSH public key without options, its blob of the type it starts with."""
    key = key.strip()
    if len(key.encode()) > MAX_KEY_BYTES or not key.isprintable():
        return False
    words = key.split(maxsplit=2)
    if len(words) < 2 or words[0] not in KEY_TYPES:
        return False
    try:
        blob = base64.b64decode(words[1], validate=True)
    except ValueError:
        return False
    size = int.from_bytes(blob[:4], "big")
    return blob[4 : 4 + size] == words[0].encode()


def valid_zone(zone: Any, root: Path) -> bool:
    """A tzdata name as the supervisor takes one, of a zone compiled on this machine."""
    if not isinstance(zone, str) or len(zone) > MAX_ZONE or not ZONE.fullmatch(zone):
        return False
    try:
        with (root / ZONEINFO / zone).open("rb") as file:
            return file.read(4) == b"TZif"
    except OSError:
        return False


def resolved_zone(zone: Any, root: Path) -> Optional[str]:
    """zone, or the zone tzdata.zi links it to, as the supervisor resolves one.

    Links such as Asia/Calcutta still name zones, but trixie leaves their files to
    tzdata-legacy.
    """
    if not isinstance(zone, str) or len(zone) > MAX_ZONE or not ZONE.fullmatch(zone):
        return None
    if valid_zone(zone, root):
        return zone
    try:
        lines = (root / ZONEINFO / "tzdata.zi").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        fields = line.split()
        if len(fields) == 3 and fields[0] == "L" and fields[2] == zone:
            return fields[1] if valid_zone(fields[1], root) else None
    return None


def not_valid(source: str, what: str) -> None:
    log.warning("%s: %s is not valid, so it is not applied", source, what)


def check_wifi(values: dict[str, Any], source: str) -> Optional[Wifi]:
    if "ssid" not in values:
        if "wifi_key" in values:
            log.warning("%s: a Wi-Fi password without a network is not applied", source)
        return None
    ssid, key = values["ssid"], values.get("wifi_key", "")
    hidden = values.get("hidden", False)
    if not valid_ssid(ssid):
        not_valid(source, "the Wi-Fi network name")
        return None
    if not valid_wifi_key(key, values.get("wifi_key_encrypted")):
        log.warning(
            "%s: the password for Wi-Fi %s is not valid, so it is not saved",
            source,
            ssid,
        )
        return None
    if not isinstance(hidden, bool):
        log.warning(
            "%s: whether Wi-Fi %s is hidden is not valid, so it is not saved",
            source,
            ssid,
        )
        return None
    return Wifi(ssid, key, hidden)


def checked_password(password: Any, encrypted: Any) -> Optional[tuple[str, bool]]:
    """The password and whether it is a crypt(3) hash, by its form when not flagged."""
    if not isinstance(password, str) or not isinstance(encrypted, (bool, type(None))):
        return None
    if encrypted is None:
        encrypted = bool(PASSWORD_HASH.fullmatch(password))
    if encrypted:
        valid = len(password) <= MAX_HASH and bool(PASSWORD_HASH.fullmatch(password))
    else:
        valid = 1 <= len(password) <= MAX_PASSWORD and password.isprintable()
    return (password, encrypted) if valid else None


def checked_keys(keys: Any, source: str) -> list[str]:
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        log.warning("%s: the SSH keys are not a list, so none is installed", source)
        return []
    if len(keys) > MAX_KEYS:
        log.warning("%s: more than %d SSH keys, so none is installed", source, MAX_KEYS)
        return []
    accepted = []
    for number, key in enumerate(keys, 1):
        if valid_public_key(key):
            accepted.append(key.strip())
        else:
            log.warning(
                "%s: SSH key %d is not a public key, so it is not installed",
                source,
                number,
            )
    return accepted


def login_name(
    values: dict[str, Any], source: str, accounts: dict[str, Passwd]
) -> Optional[str]:
    if "user" not in values:
        # Keys or a password without a user are the first user's, as on Raspberry Pi OS.
        first = [entry.name for entry in accounts.values() if entry.uid == FIRST_UID]
        if not first:
            log.warning("%s: no user to give the login to, so none is applied", source)
        return first[0] if first else None
    if not valid_user(values["user"], accounts):
        log.warning("%s: the user name cannot have a login, so none is applied", source)
        return None
    return values["user"]


def check_login(
    values: dict[str, Any], source: str, accounts: dict[str, Passwd]
) -> Optional[Login]:
    if not {"user", "password", "keys"} & values.keys():
        return None
    name = login_name(values, source, accounts)
    if name is None:
        return None
    login = Login(name)
    if "password" in values:
        encrypted = values.get("password_encrypted")
        password = checked_password(values["password"], encrypted)
        if password is None:
            log.warning(
                "%s: the password for %s is not valid, so it is not set", source, name
            )
        else:
            login.password, login.hashed = password
    if "keys" in values:
        login.keys = checked_keys(values["keys"], source)
    if "password_authentication" in values:
        if isinstance(values["password_authentication"], bool):
            login.password_authentication = values["password_authentication"]
        else:
            not_valid(source, "whether SSH takes a password")
    if login.password is None and not login.keys:
        if "password" not in values and "keys" not in values:
            log.warning(
                "%s: %s has neither a password nor a key, so no login is applied",
                source,
                name,
            )
        return None
    return login


def check(values: dict[str, Any], source: str, root: Path) -> Settings:
    """The settings in values that pass every check; each failure is logged by name."""
    settings = Settings()
    if "hostname" in values:
        if valid_hostname(values["hostname"]):
            settings.hostname = values["hostname"]
        else:
            not_valid(source, "the host name")
    if "country" in values:
        if valid_country(values["country"]):
            settings.country = values["country"]
        else:
            not_valid(source, "the Wi-Fi country")
    settings.wifi = check_wifi(values, source)
    settings.login = check_login(values, source, read_accounts(root))
    if "ssh" in values:
        if isinstance(values["ssh"], bool):
            settings.ssh = values["ssh"]
        else:
            not_valid(source, "whether SSH is on")
    if "timezone" in values:
        settings.timezone = resolved_zone(values["timezone"], root)
        if settings.timezone is None:
            not_valid(source, "the time zone")
    return settings


def read_card(boot: Path, root: Path) -> Optional[Settings]:
    for name in CLOUD_INIT:
        if (boot / name).exists():
            log.warning(
                "%s is for cloud-init, which this image does not have; it is not read",
                name,
            )
    toml, firstrun = boot / CUSTOM_TOML, boot / FIRSTRUN
    if toml.exists():
        if firstrun.exists():
            log.info("firstrun.sh is not read, as custom.toml is there")
        source, text = CUSTOM_TOML, read_text(toml)
        values = None if text is None else read_custom_toml(text)
    elif firstrun.exists():
        source, text = FIRSTRUN, read_text(firstrun)
        values = None if text is None else read_firstrun(text)
    else:
        return None
    if values is None:
        return None
    log.info("Applying the settings in %s", source)
    return check(values, source, root)


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes, mode: Optional[int] = 0o644) -> None:
    """Replaces path with data durably and whole, private until it has its mode.

    A mode of None leaves the one the filesystem gives, as FAT refuses any other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.kalinka-card")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    sync_directory(path.parent)


def with_host_name(hosts: str, name: str) -> str:
    """hosts with 127.0.1.1, Debian's address for the machine's own name, as name."""
    entry = f"127.0.1.1\t{name}"
    lines = [
        entry if line.split()[:1] == ["127.0.1.1"] else line
        for line in hosts.splitlines()
    ]
    if entry not in lines:
        lines.append(entry)
    return "".join(f"{line}\n" for line in lines)


def keyfile_string(value: str) -> str:
    """value as a GLib key file string, all spaces escaped so none at an end is lost."""
    return value.replace("\\", "\\\\").replace(" ", "\\s")


def keyfile_profile(wifi: Wifi, connection: uuid.UUID) -> str:
    """NetworkManager's keyfile for wifi, its SSID as bytes, which need no escaping."""
    lines = [
        "[connection]",
        f"id={keyfile_string(wifi.ssid)}",
        f"uuid={connection}",
        "type=wifi",
        "autoconnect=true",
        "",
        "[wifi]",
        "mode=infrastructure",
        "ssid=" + "".join(f"{byte};" for byte in wifi.ssid.encode()),
    ]
    if wifi.hidden:
        lines.append("hidden=true")
    if wifi.key:
        lines += ["", "[wifi-security]", "key-mgmt=wpa-psk"]
        lines.append(f"psk={keyfile_string(wifi.key)}")
    lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto"]
    return "".join(f"{line}\n" for line in lines)


class Applier:
    """Writes accepted settings into the tree at root, through system for the rest."""

    def __init__(self, root: Path, system: System) -> None:
        self.root = root
        self.system = system

    def apply(self, settings: Settings) -> None:
        steps = (
            ("the host name", self.hostname, settings.hostname),
            ("the Wi-Fi country", self.country, settings.country),
            ("the Wi-Fi network", self.wifi, settings.wifi),
            ("the login", self.login, settings.login),
            ("SSH", self.ssh, settings.ssh),
            ("the time zone", self.timezone, settings.timezone),
        )
        for what, step, value in steps:
            if value is None:
                continue
            try:
                step(value)
            except Exception as error:
                # strerror never quotes the input; other exceptions' text may.
                reason = getattr(error, "strerror", None) or type(error).__name__
                log.warning("Could not apply %s: %s", what, reason)

    def hostname(self, name: str) -> None:
        write_file(self.root / "etc/hostname", f"{name}\n".encode())
        hosts = self.root / "etc/hosts"
        text = hosts.read_text() if hosts.exists() else ""
        write_file(hosts, with_host_name(text, name).encode())
        # avahi takes the name the kernel has when it starts, which is after this.
        self.system.set_hostname(name)
        log.info("Host name set to %s", name)

    def country(self, code: str) -> None:
        conf = f"options cfg80211 ieee80211_regdom={code}\n"
        write_file(self.root / REGULATORY_CONF, conf.encode())
        if not self.system.run(["iw", "reg", "set", code]):
            log.info("The radio takes Wi-Fi country %s from the next start", code)
        log.info("Wi-Fi country set to %s", code)

    def wifi(self, wifi: Wifi) -> None:
        profile = keyfile_profile(wifi, uuid.uuid4())
        write_file(self.root / PROFILE, profile.encode(), 0o600)
        self.unblock_wifi()
        write_file(self.root / NETWORK_MARKER, b"")
        hidden = ", as a hidden one" if wifi.hidden else ""
        log.info("Wi-Fi network %s saved%s", wifi.ssid, hidden)

    def unblock_wifi(self) -> None:
        """Clears the Wi-Fi radios' soft block: a network on the card asks for Wi-Fi."""
        for radio in sorted((self.root / "sys/class/rfkill").glob("rfkill*")):
            try:
                if (radio / "type").read_text().strip() != "wlan":
                    continue
                if (radio / "hard").read_text().strip() == "1":
                    log.info("Wi-Fi is switched off by a hardware switch")
                elif (radio / "soft").read_text().strip() == "1":
                    (radio / "soft").write_text("0")
                    log.info("Switched Wi-Fi on to join the card's network")
            except OSError:
                log.info("Could not read or clear a radio's block")
        # systemd-rfkill restores these as a radio appears, which may be after this run.
        for saved in sorted((self.root / RFKILL_STATE).glob("*:wlan")):
            try:
                if saved.read_text().strip() != "0":
                    write_file(saved, b"0\n")
            except OSError:
                log.info("Could not clear a radio's saved block")

    def login(self, login: Login) -> None:
        account = read_accounts(self.root).get(login.name)
        created = account is None
        if created:
            comment = ["--comment", ADMIN_COMMENT] if login.name == ADMIN else []
            useradd = ["useradd", "--create-home", "--shell", "/bin/bash", *comment]
            if self.system.run([*useradd, login.name]):
                account = read_accounts(self.root).get(login.name)
            if account is None:
                log.warning("Could not create the login %s", login.name)
                return
            log.info("Login %s created", login.name)
        if not self.system.run(["usermod", "--append", "--groups", "sudo", login.name]):
            log.warning("Could not give %s administrator rights", login.name)
        if login.password is not None:
            chpasswd = ["chpasswd", "-e"] if login.hashed else ["chpasswd"]
            line = f"{login.name}:{login.password}\n"
            if self.system.run(chpasswd, stdin=line.encode()):
                log.info("Password set for %s", login.name)
            else:
                log.warning("Could not set the password for %s", login.name)
        elif created or password_locked(self.root, login.name):
            # OpenSSH refuses even a key to an account whose password is locked by '!'.
            if not self.system.run(["usermod", "-p", "*", login.name]):
                log.warning("Could not open %s to SSH keys", login.name)
        if login.keys:
            self.authorized_keys(account, login.keys)
        self.password_login(login.name, login.password_authentication)

    def authorized_keys(self, account: Passwd, keys: list[str]) -> None:
        home = self.root / account.home.lstrip("/")
        directory = home / ".ssh"
        if not home.is_dir() or directory.is_symlink():
            log.warning("%s has no home for SSH keys, so none is put in", account.name)
            return
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        self.system.chown(directory, account.uid, account.gid)
        path = directory / "authorized_keys"
        lines = []
        if path.is_file() and not path.is_symlink():
            lines = path.read_text().splitlines()
        added = [key for key in dict.fromkeys(keys) if key not in lines]
        write_file(path, "".join(f"{line}\n" for line in lines + added).encode(), 0o600)
        self.system.chown(path, account.uid, account.gid)
        log.info("%d SSH key(s) installed for %s", len(added), account.name)

    def password_login(self, name: str, allowed: Optional[bool]) -> None:
        """A Match block for name alone: the control page's login keeps password SSH."""
        path = self.root / f"etc/ssh/sshd_config.d/kalinka-card-{name}.conf"
        if allowed is False:
            match = f"Match User {name}\n\tPasswordAuthentication no\n"
            write_file(path, match.encode())
            log.info("SSH takes no password for %s", name)
        elif allowed is True:
            path.unlink(missing_ok=True)

    def ssh(self, enabled: bool) -> None:
        if not enabled:
            return
        # --no-block: sshd is ordered after this unit, which waiting for it would hang.
        started = self.system.run(["systemctl", "enable", "ssh.service"]) and (
            self.system.run(["systemctl", "--no-block", "start", "ssh.service"])
        )
        if started:
            log.info("SSH turned on")
        else:
            log.warning("Could not turn SSH on")

    def timezone(self, zone: str) -> None:
        link = self.root / "etc/localtime"
        staged = link.with_name(".localtime.kalinka-card")
        staged.unlink(missing_ok=True)
        staged.symlink_to(f"/{ZONEINFO}/{zone}")
        os.replace(staged, link)
        log.info("Time zone set to %s", zone)


def overwrite_with_zeros(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        remaining = os.fstat(fd).st_size
        block = bytes(64 * 1024)
        while remaining > 0:
            remaining -= os.write(fd, block[: min(remaining, len(block))])
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_inputs(boot: Path) -> None:
    """Removes every file Imager left, read or not: FAT cannot keep them private."""
    for name in INPUTS:
        path = boot / name
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            if not path.is_symlink() and path.is_file():
                overwrite_with_zeros(path)
            os.unlink(path)
            sync_directory(boot)
            log.info("Removed %s from the boot partition", name)
        except OSError as error:
            log.warning(
                "Could not remove %s from the boot partition: %s",
                name,
                error.strerror,
            )


def without_imager_tokens(cmdline: bytes) -> bytes:
    """cmdline without Imager's systemd.run tokens, and otherwise byte for byte."""
    return _LEADING_TOKEN.sub(b"", _TOKEN_AFTER_SPACE.sub(b"", cmdline))


def strip_imager_tokens(boot: Path) -> None:
    path = boot / "cmdline.txt"
    try:
        original = path.read_bytes()
        cleaned = without_imager_tokens(original)
        if cleaned != original:
            write_file(path, cleaned, mode=None)
            log.info("Took Imager's first-start command out of cmdline.txt")
    except FileNotFoundError:
        return
    except OSError as error:
        log.warning(
            "Could not take Imager's first-start command out of cmdline.txt: %s",
            error.strerror,
        )


def innermost_line(error: BaseException) -> int:
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    return trace.tb_lineno if trace is not None else 0


def run(root: Path, system: System) -> int:
    """Applies what the card asks for, then removes its files; 1 if any was refused."""
    refusals = Refusals()
    log.addHandler(refusals)
    boot = next((root / name for name in BOOT_DIRS if (root / name).is_dir()), None)
    try:
        settings = None if boot is None else read_card(boot, root)
        if settings is not None:
            Applier(root, system).apply(settings)
    except Exception as error:
        # The type and line only: an exception's text can quote what it was handed.
        log.warning(
            "Applying stopped at an unexpected %s, line %d",
            type(error).__name__,
            innermost_line(error),
        )
    finally:
        if boot is not None:
            remove_inputs(boot)
            # Last: a power cut before it leaves only the tokens, for the next start.
            strip_imager_tokens(boot)
        log.removeHandler(refusals)
    return 1 if refusals.count else 0


def main(argv: Optional[list[str]] = None, system: Optional[System] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help="the tree to work on instead of /, for tests",
    )
    args = parser.parse_args(argv)
    return run(args.root, system or System())


if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.setFormatter(JournalFormatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    sys.exit(main())
