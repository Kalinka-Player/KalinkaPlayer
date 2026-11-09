"""card-settings.py on fake roots: what it reads, what it applies, what it leaves.

The fixtures follow the custom.toml format raspberrypi-sys-mods documents and
the firstrun.sh Imager 1.8 writes. They were written from those formats, not
copied from a card that Imager wrote.
"""

import contextlib
import importlib.util
import io
import logging
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
OVERLAY = TESTS.parent / "overlays/debootstrap"
PROGRAM = OVERLAY / "usr/lib/kalinka-image/card-settings.py"
FIXTURES = TESTS / "fixtures/imager"

# A __pycache__ beside the program would go into the PC image with the overlay.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("card_settings", PROGRAM)
card_settings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(card_settings)
Settings, Wifi, Login = card_settings.Settings, card_settings.Wifi, card_settings.Login

PSK = "71b81377d300cb6b621dcf5fcef399c47b73d1bf524901aff20f0c3a8c87223a"
HASH = "$5$fixturesalt$J0a1n2u3a4r5y6f7i8x9t0u1r2e3h4a5s6h7f8i9x0A1B"
LAPTOP_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMTAXUCow+vA6wxO3P9dTplkpfu14khgcRqSqhZmi/vX"
    " listener@laptop"
)
PHONE_KEY = (
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAgFjZ6rLZ6SgF8jairlWgsyzkI"
    "Y3gPcRTdoKDgxVZEx8Q= listener@phone"
)
DESK_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILHpkyRQW9MtoOH4Xc9eGaCdsEgeihX2LEHrMgMEqOkn"
    " old@desk"
)
PASSPHRASE = " a back\\slash, it's; ok"
TOML_PASSPHRASE = PASSPHRASE.replace("\\", "\\\\")
PLAIN_PASSWORD = "correct horse battery staple"
SECRETS = (
    PSK,
    HASH,
    PASSPHRASE.strip(),
    PLAIN_PASSWORD,
    *(key.split()[1] for key in (LAPTOP_KEY, PHONE_KEY, DESK_KEY)),
)

CMDLINE = b"console=tty1 root=PARTUUID=0c1a2b3c-02 rootfstype=ext4 rootwait quiet"
IMAGER_EDIT = (
    b" systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot"
    b" systemd.unit=kernel-command-line.target"
)

PASSWD = """\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
kalusr:x:995:995::/var/lib/kalinka:/usr/sbin/nologin
kalinka:x:1000:1000:,,,:/home/kalinka:/bin/bash
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
"""
SHADOW = "root:*:20000:0:99999:7:::\nkalinka:!:20000:0:99999:7:::\n"
HOSTS = "127.0.0.1\tlocalhost\n127.0.1.1\tkalinka\n::1\tlocalhost ip6-localhost\n"

FIXTURE_SETTINGS = Settings(
    hostname="livingroom",
    country="GB",
    wifi=Wifi("Home Network", PSK, False),
    login=Login("listener", HASH, True, [LAPTOP_KEY, PHONE_KEY], False),
    ssh=True,
    timezone="Europe/London",
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def firstrun_line(*words: str) -> str:
    """A call as Imager writes it into firstrun.sh, its arguments quoted."""
    return "   " + " ".join([card_settings.IMAGER_CUSTOM, *map(shlex.quote, words)])


class FakeRoot:
    """A Pi image's root as its first start finds it, in a temporary directory."""

    def __init__(self, test: unittest.TestCase, boot: str = "boot/firmware") -> None:
        self.path = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.path)
        self.boot = self.path / boot
        self.boot.mkdir(parents=True)
        self.write("etc/passwd", PASSWD)
        self.write("etc/shadow", SHADOW)
        self.write("etc/hostname", "kalinka\n")
        self.write("etc/hosts", HOSTS)
        (self.path / "home/kalinka").mkdir(parents=True)
        (self.path / "etc/ssh/sshd_config.d").mkdir(parents=True)
        self.write_bytes("usr/share/zoneinfo/Europe/London", b"TZif2" + bytes(40))
        self.write_bytes("usr/share/zoneinfo/Etc/NotAZone", b"Europe/London\n")
        for radio, kind in (("rfkill0", "wlan"), ("rfkill1", "bluetooth")):
            self.write(f"sys/class/rfkill/{radio}/type", f"{kind}\n")
            self.write(f"sys/class/rfkill/{radio}/soft", "1\n")
            self.write(f"sys/class/rfkill/{radio}/hard", "0\n")
        (self.boot / "cmdline.txt").write_bytes(CMDLINE + IMAGER_EDIT + b"\n")

    def write(self, path: str, text: str) -> Path:
        return self.write_bytes(path, text.encode())

    def write_bytes(self, path: str, data: bytes) -> Path:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def read(self, path: str) -> str:
        return (self.path / path).read_text()

    def card(self, name: str, text: str) -> Path:
        path = self.boot / name
        path.write_text(text)
        return path

    def left_on_card(self) -> list[str]:
        return sorted(os.listdir(self.boot))


class Recorder:
    """Stands in for the machine: records commands, adds the accounts useradd would."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.stdin: dict[tuple[str, ...], bytes] = {}
        self.owners: dict[Path, tuple[int, int]] = {}
        self.hostname = None
        self.failing: set[str] = set()

    def run(self, argv: list[str], stdin: bytes = None) -> bool:
        self.calls.append(list(argv))
        if stdin is not None:
            self.stdin[tuple(argv)] = stdin
        if argv[0] in self.failing:
            return False
        if argv[0] == "useradd":
            self.add_account(argv[-1])
        return True

    def add_account(self, name: str) -> None:
        passwd = self.root / "etc/passwd"
        uids = [int(line.split(":")[2]) for line in passwd.read_text().splitlines()]
        uid = max(uid for uid in uids if uid < 60000) + 1
        with passwd.open("a") as file:
            file.write(f"{name}:x:{uid}:{uid}::/home/{name}:/bin/bash\n")
        with (self.root / "etc/shadow").open("a") as file:
            file.write(f"{name}:!:20000:0:99999:7:::\n")
        (self.root / "home" / name).mkdir(parents=True)

    def chown(self, path: Path, uid: int, gid: int) -> None:
        self.owners[Path(path)] = (uid, gid)

    def set_hostname(self, name: str) -> None:
        self.hostname = name

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


class Journal(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


class CardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = FakeRoot(self)
        self.recorder = Recorder(self.root.path)
        self.journal: list[str] = []
        self.printed = ""

    @contextlib.contextmanager
    def capture(self):
        journal = Journal()
        logger = card_settings.log
        level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(journal)
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                with contextlib.redirect_stderr(printed):
                    yield
        finally:
            logger.removeHandler(journal)
            logger.setLevel(level)
            self.journal = journal.lines
            self.printed = printed.getvalue()

    def run_program(self) -> int:
        with self.capture():
            argv = ["--root", str(self.root.path)]
            return card_settings.main(argv, system=self.recorder)

    def settings(self) -> Settings:
        with self.capture():
            return card_settings.read_card(self.root.boot, self.root.path)

    def warnings(self) -> list[str]:
        return [line for line in self.journal if line.startswith("WARNING")]

    def assertJournal(self, text: str) -> None:
        self.assertTrue(
            any(text in line for line in self.journal),
            f"{text!r} not in the journal:\n" + "\n".join(self.journal),
        )

    def toml(self, text: str) -> Path:
        return self.root.card("custom.toml", "config_version = 1\n" + text)

    def wlan(self, extra: str = "", ssid: str = "Home", key: str = PSK) -> Path:
        return self.toml(f'[wlan]\nssid = "{ssid}"\npassword = "{key}"\n{extra}')


class ReadingTest(CardTest):
    def test_the_custom_toml_fixture_gives_every_setting(self):
        self.root.card("custom.toml", fixture("custom.toml"))
        self.assertEqual(self.settings(), FIXTURE_SETTINGS)
        self.assertEqual(self.warnings(), [])
        self.assertJournal("the keyboard layout is not applied")

    def test_the_firstrun_fixture_gives_the_same_settings(self):
        self.root.card("firstrun.sh", fixture("firstrun.sh"))
        self.assertEqual(self.settings(), FIXTURE_SETTINGS)
        self.assertEqual(self.warnings(), [])
        self.assertJournal("the keyboard layout is not applied")

    def test_custom_toml_is_read_when_firstrun_sh_is_there_too(self):
        self.root.card("custom.toml", fixture("custom.toml"))
        self.root.card("firstrun.sh", firstrun_line("set_hostname", "attic") + "\n")
        self.assertEqual(self.settings().hostname, "livingroom")
        self.assertJournal("firstrun.sh is not read, as custom.toml is there")

    def test_a_bom_and_crlf_line_ends_read_as_the_plain_file(self):
        text = fixture("custom.toml").replace("\n", "\r\n")
        (self.root.boot / "custom.toml").write_bytes(b"\xef\xbb\xbf" + text.encode())
        self.assertEqual(self.settings(), FIXTURE_SETTINGS)

    def test_malformed_toml_is_named_by_line_without_its_contents(self):
        self.toml(f'[wlan]\nssid = "Home"\npassword = "{PSK}\n')
        self.assertIsNone(self.settings())
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("custom.toml is not valid TOML (line 4)", self.warnings()[0])
        self.assertNotIn(PSK, "\n".join(self.journal))

    def test_a_file_that_is_not_utf8_is_refused(self):
        (self.root.boot / "custom.toml").write_bytes(b'[system]\nhostname = "\xff"\n')
        self.assertIsNone(self.settings())
        self.assertJournal("custom.toml is not UTF-8 text")

    def test_another_config_version_refuses_the_file(self):
        self.root.card("custom.toml", 'config_version = 2\n[system]\nhostname = "a"\n')
        self.assertIsNone(self.settings())
        self.assertJournal("config_version this image does not read")

    def test_values_of_the_wrong_type_are_refused_one_by_one(self):
        self.toml(
            "[system]\nhostname = 5\n"
            f'[wlan]\nssid = "Home"\npassword = "{PSK}"\nhidden = "yes"\n'
            f'[ssh]\nenabled = "yes"\nauthorized_keys = "{LAPTOP_KEY}"\n'
            '[locale]\ntimezone = "Europe/London"\n'
        )
        self.assertEqual(self.settings(), Settings(timezone="Europe/London"))
        self.assertEqual(len(self.warnings()), 4)

    def test_a_section_that_is_not_a_table_is_refused(self):
        self.toml('wlan = "Home"\n[system]\nhostname = "attic"\n')
        self.assertEqual(self.settings(), Settings(hostname="attic"))
        self.assertJournal("[wlan] is not a table")

    def test_host_names_that_are_not_valid_are_refused(self):
        names = ("-attic", "attic-", "at_tic", "at tic", "", "a" * 64, "attic.local")
        for name in names:
            with self.subTest(name=name):
                self.toml(f'[system]\nhostname = "{name}"\n')
                self.assertIsNone(self.settings().hostname)
                self.assertJournal("the host name is not valid")

    def test_a_host_name_of_63_characters_is_taken(self):
        self.toml(f'[system]\nhostname = "{"a" * 63}"\n')
        self.assertEqual(self.settings().hostname, "a" * 63)

    def test_an_ssid_of_more_than_32_bytes_is_refused(self):
        cases = (
            ("x" * 32, True),
            ("x" * 33, False),
            ("é" * 16, True),
            ("é" * 17, False),
        )
        for ssid, taken in cases:
            with self.subTest(ssid=ssid):
                self.wlan(ssid=ssid)
                self.assertEqual(self.settings().wifi is not None, taken)

    def test_an_ssid_with_a_control_character_is_refused(self):
        self.wlan(ssid="Home\\nNetwork")
        self.assertIsNone(self.settings().wifi)

    def test_a_key_marked_encrypted_must_be_64_hex_digits(self):
        self.wlan("password_encrypted = true\n", key=PSK[:63])
        self.assertIsNone(self.settings().wifi)
        self.assertJournal("the password for Wi-Fi Home is not valid")

    def test_wifi_passwords_that_wpa_cannot_take_are_refused(self):
        for key in ("seven77", "x" * 64, "café-café", "tab\there"):
            with self.subTest(key=key):
                line = firstrun_line("set_wlan", "Home", key)
                self.root.card("firstrun.sh", line + "\n")
                self.assertIsNone(self.settings().wifi)

    def test_countries_that_are_not_two_capitals_are_refused(self):
        for country in ('"gb"', '"GBR"', "44"):
            with self.subTest(country=country):
                self.wlan(f"country = {country}\n")
                settings = self.settings()
                self.assertIsNone(settings.country)
                self.assertIsNotNone(settings.wifi)

    def test_system_and_reserved_accounts_get_no_login(self):
        for name in ("root", "kalusr", "daemon", "nobody", "Listener", "1listener"):
            with self.subTest(name=name):
                self.toml(f'[user]\nname = "{name}"\npassword = "{HASH}"\n')
                self.assertIsNone(self.settings().login)
                self.assertJournal("the user name cannot have a login")

    def test_a_hash_with_a_newline_is_refused(self):
        self.toml(
            f'[user]\nname = "listener"\npassword = "{HASH}\\n"\n'
            "password_encrypted = true\n"
        )
        self.assertIsNone(self.settings().login)
        self.assertJournal("the password for listener is not valid")

    def test_a_plain_password_marked_encrypted_is_refused(self):
        self.toml(
            f'[user]\nname = "listener"\npassword = "{PLAIN_PASSWORD}"\n'
            "password_encrypted = true\n"
        )
        self.assertIsNone(self.settings().login)

    def test_a_plain_password_is_taken_when_marked_plain(self):
        self.toml(
            f'[user]\nname = "listener"\npassword = "{PLAIN_PASSWORD}"\n'
            "password_encrypted = false\n"
        )
        login = Login("listener", PLAIN_PASSWORD, False)
        self.assertEqual(self.settings().login, login)

    def test_a_key_of_an_unknown_type_is_refused_and_the_rest_kept(self):
        dsa = "ssh-dss " + LAPTOP_KEY.split()[1] + " old@laptop"
        self.toml(f'[ssh]\nauthorized_keys = ["{dsa}", "{PHONE_KEY}"]\n')
        self.assertEqual(self.settings().login, Login("kalinka", keys=[PHONE_KEY]))
        self.assertJournal("SSH key 1 is not a public key")

    def test_a_key_whose_blob_is_another_type_is_refused(self):
        mislabelled = "ssh-rsa " + LAPTOP_KEY.split()[1]
        self.toml(f'[ssh]\nauthorized_keys = ["{mislabelled}", "truncated AAAA"]\n')
        self.assertIsNone(self.settings().login)
        self.assertEqual(len(self.warnings()), 2)

    def test_more_than_16_keys_are_refused(self):
        keys = ", ".join(f'"{LAPTOP_KEY}{n}"' for n in range(17))
        self.toml(f"[ssh]\nauthorized_keys = [{keys}]\n")
        self.assertIsNone(self.settings().login)
        self.assertJournal("more than 16 SSH keys")

    def test_zones_that_leave_tzdata_or_are_not_compiled_are_refused(self):
        zones = (
            "../../etc/passwd",
            "Europe/../Europe/London",
            "/Europe/London",
            "Etc/NotAZone",
            "Mars/Olympus",
        )
        for zone in zones:
            with self.subTest(zone=zone):
                self.toml(f'[locale]\ntimezone = "{zone}"\n')
                self.assertIsNone(self.settings().timezone)
                self.assertJournal("the time zone is not valid")

    def test_a_link_is_resolved_to_its_zone_as_the_supervisor_does(self):
        too_long = "A" * (card_settings.MAX_ZONE + 1)
        self.root.write(
            "usr/share/zoneinfo/tzdata.zi",
            "# version 2025b\nL Europe/London GB-Eire\nL Mars/Olympus Asia/Olympus\n"
            f"L Europe/London {too_long}\n",
        )
        self.toml('[locale]\ntimezone = "GB-Eire"\n')
        self.assertEqual(self.settings().timezone, "Europe/London")
        for zone in ("Asia/Olympus", too_long):
            with self.subTest(zone=zone):
                self.toml(f'[locale]\ntimezone = "{zone}"\n')
                self.assertIsNone(self.settings().timezone)
                self.assertJournal("the time zone is not valid")

    def test_a_line_that_does_not_tokenise_is_named_and_the_rest_applied(self):
        call = f"   {card_settings.IMAGER_CUSTOM} set_wlan 'Home Network' '{PSK}'"
        broken = call.removesuffix("'")
        self.assertIn(f"{call} 'GB'\n", fixture("firstrun.sh"))
        text = fixture("firstrun.sh").replace(f"{call} 'GB'", broken)
        self.root.card("firstrun.sh", text)
        settings = self.settings()
        self.assertIsNone(settings.wifi)
        self.assertEqual(settings.hostname, "livingroom")
        self.assertEqual(settings.login, FIXTURE_SETTINGS.login)
        number = text.splitlines().index(broken) + 1
        self.assertEqual(
            self.warnings(),
            [
                f"WARNING firstrun.sh line {number} cannot be read, "
                "so what it sets is not applied"
            ],
        )

    def test_set_wlan_with_hidden_and_without_a_country(self):
        line = firstrun_line("set_wlan", "-h", "Attic", "passphrase1")
        self.root.card("firstrun.sh", line + "\n")
        settings = self.settings()
        self.assertEqual(settings.wifi, Wifi("Attic", "passphrase1", True))
        self.assertIsNone(settings.country)

    def test_enable_ssh_without_keys_keeps_password_login(self):
        text = firstrun_line("enable_ssh") + "\n"
        text += f"   {card_settings.USERCONF} 'listener' '{HASH}'\n"
        self.root.card("firstrun.sh", text)
        settings = self.settings()
        self.assertTrue(settings.ssh)
        self.assertEqual(settings.login, Login("listener", HASH, True, [], True))

    def test_keys_without_a_user_go_to_the_first_user(self):
        line = firstrun_line("enable_ssh", "-k", LAPTOP_KEY)
        self.root.card("firstrun.sh", line + "\n")
        login = Login("kalinka", keys=[LAPTOP_KEY], password_authentication=False)
        self.assertEqual(self.settings().login, login)

    def test_a_firstrun_without_imager_calls_is_refused(self):
        self.root.card("firstrun.sh", "#!/bin/bash\necho hello\n")
        self.assertEqual(self.settings(), Settings())
        self.assertJournal("firstrun.sh holds none of the settings this image reads")

    def test_what_is_not_applied_is_named_without_its_value(self):
        self.toml(
            '[rpi_connect]\ntoken = "connect-token-value"\n'
            '[system]\nhostname = "attic"\nsecret = 1\n'
        )
        self.assertEqual(self.settings(), Settings(hostname="attic"))
        self.assertJournal("custom.toml: rpi_connect is not applied")
        self.assertJournal("custom.toml: system.secret is not applied")
        self.assertNotIn("connect-token-value", "\n".join(self.journal))
        self.assertEqual(self.warnings(), [])


class ApplyingTest(CardTest):
    def test_the_full_fixture_is_applied(self):
        self.root.card("custom.toml", fixture("custom.toml"))
        self.assertEqual(self.run_program(), 0, "\n".join(self.journal))
        root = self.root

        self.assertEqual(root.read("etc/hostname"), "livingroom\n")
        hosts = HOSTS.replace("127.0.1.1\tkalinka", "127.0.1.1\tlivingroom")
        self.assertNotEqual(hosts, HOSTS)
        self.assertEqual(root.read("etc/hosts"), hosts)
        self.assertEqual(self.recorder.hostname, "livingroom")

        self.assertEqual(
            (root.path / "etc/modprobe.d/kalinka-regdom.conf").read_bytes(),
            b"options cfg80211 ieee80211_regdom=GB\n",
        )
        profile = root.path / card_settings.PROFILE
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
        text = profile.read_text()
        self.assertIn("[connection]\nid=Home\\sNetwork\n", text)
        self.assertIn("type=wifi\nautoconnect=true\n", text)
        ssid = "ssid=72;111;109;101;32;78;101;116;119;111;114;107;"
        self.assertIn(f"[wifi]\nmode=infrastructure\n{ssid}\n", text)
        self.assertNotIn("hidden", text)
        self.assertIn(f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={PSK}\n", text)
        self.assertTrue(text.endswith("[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"))
        self.assertTrue((root.path / card_settings.NETWORK_MARKER).exists())
        self.assertEqual(root.read("sys/class/rfkill/rfkill0/soft"), "0")
        self.assertEqual(root.read("sys/class/rfkill/rfkill1/soft"), "1\n")

        self.assertEqual(
            self.recorder.commands(),
            [
                "iw reg set GB",
                "useradd --create-home --shell /bin/bash listener",
                "usermod --append --groups sudo listener",
                "chpasswd -e",
                "systemctl enable ssh.service",
                "systemctl --no-block start ssh.service",
            ],
        )
        chpasswd = self.recorder.stdin[("chpasswd", "-e")]
        self.assertEqual(chpasswd, f"listener:{HASH}\n".encode())
        home = root.path / "home/listener"
        keys = home / ".ssh/authorized_keys"
        self.assertEqual(keys.read_text(), f"{LAPTOP_KEY}\n{PHONE_KEY}\n")
        self.assertEqual(stat.S_IMODE(keys.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((home / ".ssh").stat().st_mode), 0o700)
        owners = {home / ".ssh": (1001, 1001), keys: (1001, 1001)}
        self.assertEqual(self.recorder.owners, owners)
        self.assertEqual(
            root.read("etc/ssh/sshd_config.d/kalinka-card-listener.conf"),
            "Match User listener\n\tPasswordAuthentication no\n",
        )
        zone = os.readlink(root.path / "etc/localtime")
        self.assertEqual(zone, "/usr/share/zoneinfo/Europe/London")

        self.assertEqual(root.left_on_card(), ["cmdline.txt"])
        self.assertEqual((root.boot / "cmdline.txt").read_bytes(), CMDLINE + b"\n")
        self.assertEqual(list((root.path / "etc").rglob(".*.kalinka-card")), [])

    def test_kalinka_admin_is_made_as_the_control_page_makes_it(self):
        self.toml(f'[user]\nname = "kalinka-admin"\npassword = "{HASH}"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(
            self.recorder.calls[0],
            [
                "useradd",
                "--create-home",
                "--shell",
                "/bin/bash",
                "--comment",
                "Kalinka dashboard administrator",
                "kalinka-admin",
            ],
        )

    def test_the_first_user_is_updated_not_made_again(self):
        self.toml(f'[user]\nname = "kalinka"\npassword = "{HASH}"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(
            self.recorder.commands(),
            ["usermod --append --groups sudo kalinka", "chpasswd -e"],
        )
        chpasswd = self.recorder.stdin[("chpasswd", "-e")]
        self.assertEqual(chpasswd, f"kalinka:{HASH}\n".encode())

    def test_a_plain_password_goes_to_chpasswd_to_hash(self):
        self.toml(
            f'[user]\nname = "listener"\npassword = "{PLAIN_PASSWORD}"\n'
            "password_encrypted = false\n"
        )
        self.assertEqual(self.run_program(), 0)
        self.assertIn(["chpasswd"], self.recorder.calls)
        chpasswd = self.recorder.stdin[("chpasswd",)]
        self.assertEqual(chpasswd, f"listener:{PLAIN_PASSWORD}\n".encode())

    def test_a_key_only_login_is_opened_to_keys_and_merged(self):
        keys = f"{DESK_KEY}\n{LAPTOP_KEY}\n"
        self.root.write("home/kalinka/.ssh/authorized_keys", keys)
        self.toml(f'[ssh]\nauthorized_keys = ["{LAPTOP_KEY}", "{PHONE_KEY}"]\n')
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(
            self.recorder.commands(),
            ["usermod --append --groups sudo kalinka", "usermod -p * kalinka"],
        )
        self.assertEqual(
            self.root.read("home/kalinka/.ssh/authorized_keys"),
            f"{DESK_KEY}\n{LAPTOP_KEY}\n{PHONE_KEY}\n",
        )

    def test_a_new_key_only_login_is_opened_to_keys(self):
        self.toml(
            f'[user]\nname = "listener"\n[ssh]\nauthorized_keys = ["{LAPTOP_KEY}"]\n'
        )
        self.assertEqual(self.run_program(), 0)
        self.assertIn(["usermod", "-p", "*", "listener"], self.recorder.calls)
        self.assertFalse(any(call[0] == "chpasswd" for call in self.recorder.calls))

    def test_keys_leave_a_password_already_set_alone(self):
        self.root.write("etc/shadow", f"kalinka:{HASH}:20000:0:99999:7:::\n")
        self.toml(f'[ssh]\nauthorized_keys = ["{LAPTOP_KEY}"]\n')
        self.assertEqual(self.run_program(), 0)
        self.assertNotIn(["usermod", "-p", "*", "kalinka"], self.recorder.calls)

    def test_password_login_is_switched_off_only_when_asked(self):
        dropins = "etc/ssh/sshd_config.d"
        dropin = self.root.write(f"{dropins}/kalinka-card-listener.conf", "old\n")
        self.toml(
            f'[user]\nname = "listener"\npassword = "{HASH}"\n'
            "[ssh]\nenabled = true\npassword_authentication = true\n"
        )
        self.assertEqual(self.run_program(), 0)
        self.assertFalse(dropin.exists())
        self.assertEqual(list((self.root.path / dropins).iterdir()), [])

    def test_no_network_on_the_card_leaves_no_profile_and_no_marker(self):
        self.toml('[system]\nhostname = "attic"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertFalse((self.root.path / card_settings.PROFILE).exists())
        self.assertFalse((self.root.path / card_settings.NETWORK_MARKER).exists())
        self.assertEqual(self.root.read("sys/class/rfkill/rfkill0/soft"), "1\n")

    def test_a_network_clears_the_block_systemd_restores_later(self):
        for name in ("platform-wifi:wlan", "platform-bt:bluetooth"):
            self.root.write(f"var/lib/systemd/rfkill/{name}", "1\n")
        self.wlan(ssid="Cafe", key="")
        self.assertEqual(self.run_program(), 0, "\n".join(self.journal))
        state = "var/lib/systemd/rfkill"
        self.assertEqual(self.root.read(f"{state}/platform-wifi:wlan"), "0\n")
        self.assertEqual(self.root.read(f"{state}/platform-bt:bluetooth"), "1\n")

    def test_no_network_leaves_the_saved_block(self):
        saved = "var/lib/systemd/rfkill/platform-wifi:wlan"
        self.root.write(saved, "1\n")
        self.toml('[system]\nhostname = "attic"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(self.root.read(saved), "1\n")

    def test_an_open_network_has_no_security_section(self):
        self.wlan(ssid="Cafe", key="")
        self.assertEqual(self.run_program(), 0)
        text = (self.root.path / card_settings.PROFILE).read_text()
        self.assertNotIn("wifi-security", text)
        self.assertNotIn("psk", text)

    def test_ssid_and_passphrase_reach_networkmanager_unchanged(self):
        ssid = " it's; café"
        line = firstrun_line("set_wlan", "-h", ssid, PASSPHRASE, "DE")
        self.root.card("firstrun.sh", line + "\n")
        self.assertEqual(self.run_program(), 0, "\n".join(self.journal))
        text = (self.root.path / card_settings.PROFILE).read_text()
        self.assertIn("ssid=" + "".join(f"{b};" for b in ssid.encode()) + "\n", text)
        self.assertIn("id=\\sit's;\\scafé\n", text)
        self.assertIn("psk=\\sa\\sback\\\\slash,\\sit's;\\sok\n", text)
        self.assertIn("hidden=true\n", text)
        self.assertJournal("Wi-Fi network  it's; café saved, as a hidden one")

    def test_a_failed_useradd_is_a_failure_and_the_rest_still_applies(self):
        self.recorder.failing.add("useradd")
        self.root.card("custom.toml", fixture("custom.toml"))
        self.assertEqual(self.run_program(), 1)
        self.assertJournal("Could not create the login listener")
        self.assertEqual(self.root.read("etc/hostname"), "livingroom\n")
        self.assertIn(["systemctl", "enable", "ssh.service"], self.recorder.calls)
        self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])

    def test_a_hosts_file_that_is_not_utf8_stops_only_the_host_name(self):
        self.root.write_bytes("etc/hosts", b"127.0.1.1\t\xff\n")
        self.root.card("custom.toml", fixture("custom.toml"))
        self.assertEqual(self.run_program(), 1)
        self.assertJournal("Could not apply the host name: UnicodeDecodeError")
        self.assertIn(["systemctl", "enable", "ssh.service"], self.recorder.calls)
        zone = os.readlink(self.root.path / "etc/localtime")
        self.assertEqual(zone, "/usr/share/zoneinfo/Europe/London")

    def test_a_command_that_hangs_is_a_failure(self):
        with mock.patch.object(card_settings, "COMMAND_TIMEOUT", 0.1):
            self.assertFalse(card_settings.System().run(["sleep", "5"]))

    def test_a_country_the_radio_will_not_take_now_still_counts(self):
        self.recorder.failing.add("iw")
        self.wlan('country = "GB"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertJournal("The radio takes Wi-Fi country GB from the next start")

    def test_the_pc_image_reads_its_efi_partition(self):
        self.root = FakeRoot(self, boot="boot/efi")
        self.recorder = Recorder(self.root.path)
        self.toml('[system]\nhostname = "study"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(self.root.read("etc/hostname"), "study\n")
        self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])


class CleanupTest(CardTest):
    def test_every_input_is_zeroed_on_disk_before_it_is_unlinked(self):
        self.root.card("custom.toml", fixture("custom.toml"))
        self.root.card("firstrun.sh", fixture("firstrun.sh"))
        self.root.card("user-data", f"#cloud-config\npassword: {PLAIN_PASSWORD}\n")
        self.root.card("network-config", f"wifis:\n  password: {PSK}\n")
        sizes = {path.name: path.stat().st_size for path in self.root.boot.iterdir()}
        seen = {}
        unlink = os.unlink

        def check_then_unlink(path, *args, **kwargs):
            path = Path(path)
            if path.parent == self.root.boot:
                seen[path.name] = path.read_bytes()
            return unlink(path, *args, **kwargs)

        with mock.patch.object(card_settings.os, "unlink", wraps=check_then_unlink):
            self.assertEqual(self.run_program(), 1)
        inputs = ["custom.toml", "firstrun.sh", "network-config", "user-data"]
        self.assertEqual(sorted(seen), inputs)
        for name, data in seen.items():
            self.assertEqual(data, bytes(sizes[name]), name)
        self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])

    def test_cloud_init_files_are_removed_unread(self):
        self.root.card("user-data", "#cloud-config\nhostname: cloudy\n")
        self.assertEqual(self.run_program(), 1)
        self.assertEqual(self.root.read("etc/hostname"), "kalinka\n")
        self.assertJournal("user-data is for cloud-init, which this image does not")
        self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])

    def test_imager_tokens_leave_cmdline_txt_as_it_was_built(self):
        self.toml('[system]\nhostname = "attic"\n')
        self.run_program()
        self.assertEqual((self.root.boot / "cmdline.txt").read_bytes(), CMDLINE + b"\n")

    def test_a_clean_cmdline_txt_is_not_rewritten(self):
        cmdline = self.root.boot / "cmdline.txt"
        cmdline.write_bytes(CMDLINE + b"\n")
        before = cmdline.stat().st_ino
        self.toml('[system]\nhostname = "attic"\n')
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(cmdline.read_bytes(), CMDLINE + b"\n")
        self.assertEqual(cmdline.stat().st_ino, before)

    def test_tokens_are_taken_out_wherever_they_stand_and_nothing_else(self):
        cases = {
            b"systemd.unit=kernel-command-line.target console=tty1"
            b" systemd.run_success_action=reboot root=PARTUUID=x"
            b" systemd.run=/boot/firmware/firstrun.sh\trootwait\n": (
                b"console=tty1 root=PARTUUID=x\trootwait\n"
            ),
            b"console=tty1 systemd.run_failure_action=none"
            b" systemd.unit=multi-user.target": (
                b"console=tty1 systemd.unit=multi-user.target"
            ),
            b"console=tty1 systemd.runner=1 my.systemd.run=x quiet\n": (
                b"console=tty1 systemd.runner=1 my.systemd.run=x quiet\n"
            ),
            b"systemd.run=/boot/firstrun.sh\n": b"\n",
        }
        for before, after in cases.items():
            with self.subTest(before=before):
                self.assertEqual(card_settings.without_imager_tokens(before), after)

    def test_a_malformed_file_fails_the_run_and_is_still_removed(self):
        self.toml(f'[user]\nname = "listener"\npassword = "{HASH}\n')
        self.root.card("firstrun.sh", fixture("firstrun.sh"))
        self.assertEqual(self.run_program(), 1)
        self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])
        self.assertEqual((self.root.boot / "cmdline.txt").read_bytes(), CMDLINE + b"\n")
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(self.root.read("etc/hostname"), "kalinka\n")

    def test_a_card_with_only_the_cmdline_edit_left_is_cleaned(self):
        self.assertEqual(self.run_program(), 0)
        self.assertEqual((self.root.boot / "cmdline.txt").read_bytes(), CMDLINE + b"\n")
        self.assertEqual(self.recorder.calls, [])

    def test_a_card_without_settings_is_left_alone(self):
        (self.root.boot / "cmdline.txt").write_bytes(CMDLINE + b"\n")
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(self.journal, [])
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(self.root.read("etc/hostname"), "kalinka\n")

    def test_a_root_without_a_boot_partition_does_nothing(self):
        shutil.rmtree(self.root.boot)
        self.assertEqual(self.run_program(), 0)
        self.assertEqual(self.journal, [])

    def test_an_unexpected_error_still_removes_the_files(self):
        self.root.card("custom.toml", fixture("custom.toml"))
        with mock.patch.object(card_settings, "check", side_effect=TypeError(PSK)):
            self.assertEqual(self.run_program(), 1)
        self.assertJournal("Applying stopped at an unexpected TypeError")
        self.assertNotIn(PSK, "\n".join(self.journal))
        self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])


class SecretTest(CardTest):
    """No password, key, hash or passphrase reaches the journal, the output or argv."""

    # Each card, and the exit status that shows it was read as far as it was meant to.
    CARDS = {
        "the custom.toml fixture": ("custom.toml", fixture("custom.toml"), 0),
        "the firstrun.sh fixture": ("firstrun.sh", fixture("firstrun.sh"), 0),
        "plain secrets": (
            "custom.toml",
            'config_version = 1\n[user]\nname = "listener"\n'
            f'password = "{PLAIN_PASSWORD}"\npassword_encrypted = false\n'
            f'[wlan]\nssid = "Home"\npassword = "{TOML_PASSPHRASE}"\n'
            f'[ssh]\nauthorized_keys = ["{DESK_KEY}"]\n',
            0,
        ),
        "secrets where names belong": (
            "custom.toml",
            f'config_version = 1\n[system]\nhostname = "{PSK}"\n[user]\n'
            f'name = "{PLAIN_PASSWORD}"\npassword = "{HASH}"\n[wlan]\nssid = "Home"\n'
            f'password = "{HASH}"\n[locale]\ntimezone = "{PSK}"\n[{PSK}]\n'
            f'"{PLAIN_PASSWORD}" = 1\n',
            1,
        ),
        "malformed TOML": (
            "custom.toml",
            f'config_version = 1\n[wlan]\npassword = "{PSK}\n'
            f'[user]\npassword = "{HASH}"\n',
            1,
        ),
        "a broken firstrun.sh line": (
            "firstrun.sh",
            f"   {card_settings.USERCONF} 'listener' '{HASH}\n"
            + firstrun_line("set_wlan", "Home", PASSPHRASE)
            + "\n"
            + firstrun_line("enable_ssh", "-k", f"ssh-dss {DESK_KEY.split()[1]}")
            + "\n",
            1,
        ),
    }

    def test_no_secret_is_logged_printed_or_passed_as_an_argument(self):
        for case, (name, text, status) in self.CARDS.items():
            with self.subTest(case=case):
                self.setUp()
                self.root.card(name, text)
                self.assertEqual(self.run_program(), status, "\n".join(self.journal))
                exposed = [*self.journal, self.printed, *self.recorder.commands()]
                for secret in SECRETS:
                    self.assertNotIn(secret, "\n".join(exposed))
                self.assertEqual(self.root.left_on_card(), ["cmdline.txt"])


if __name__ == "__main__":
    unittest.main()
