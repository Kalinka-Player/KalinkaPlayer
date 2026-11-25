"""install-release.sh on a throwaway system, step after step, as its owner runs it.

system.sh runs this inside the container once the system is set up. A system's
steps share the machine, so each starts from what the one before left. The
first check that fails ends the run with ``FAIL <step>: <what>``.
"""

import contextlib
import grp
import json
import os
import platform
import pwd
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parents[1] / "scripts" / "install-release.sh"
REPO = os.environ.get("KALINKA_REPO") or "Kalinka-Player/KalinkaPlayer"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
# Reading it costs none of the quota it reports.
RATE_LIMIT_URL = "https://api.github.com/rate_limit"
# The feed here, then the bundle, the browser player and the renderer for each of
# debian's two runs, the most any system looks up.
API_CALLS = 1 + 2 * 3
RESET_SLACK = 5
QUOTA_TIMEOUT = 65 * 60
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SYSTEMCTL_CALLS = Path("/usr/bin/systemctl.calls")
APT_GET = Path("/usr/local/sbin/apt-get")
BLOCKED_DEPENDENCY = "python3-venv"
BLOCK = Path(f"/etc/apt/preferences.d/install-test-blocks-{BLOCKED_DEPENDENCY}")
SERVER_UNIT = Path("/etc/systemd/system/kalinka.service")
SERVER_ID = Path("/var/lib/kalinka/server_id")
VERSION_URL = "http://127.0.0.1:8000/server/version"
# Port 9 is discard: nothing answers there.
UNREACHABLE_INSTALLER = "http://127.0.0.1:9/install-renderer.sh"

INSTALL_TIMEOUT = 30 * 60
BOOTSTRAP_TIMEOUT = 20 * 60
ANSWER_TIMEOUT = 5 * 60

# What install-release.sh says at the turns the cases look for.
DONE = ">> Done. {tag} installed."
FALLBACK = ">> apt-get install failed; falling back to dpkg -i + apt-get -f install"
RENDERER_STEP = ">> Installing the renderer on this machine"
RENDERER_WARNING = ">> note: the renderer could not be installed"
APP_LOOKUP = ">> Looking up the latest app packages"
RELEASE_LOOKUP = ">> Looking up"
MISSING_EXTRA = "could not be installed, so"
MISSING_DEB = "did not install: dpkg reports it as"
SAID_DONE = ">> Done."
OLD_PYTHON = "error: Kalinka needs Python 3.11 or newer, and {system} has Python {python}."
SUPPORTED = (
    "It installs on Debian 13, Raspberry Pi OS (64-bit) or DietPi on Debian 13, "
    "or Ubuntu 26.04"
)
SERVER_NOT_INSTALLED = "error: {tag} is not installed: dpkg reports kalinka-server as"


class CheckFailed(Exception):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailed(message)


@contextlib.contextmanager
def step(title):
    print(f"\n=== {title}", flush=True)
    try:
        yield
    # A file or command the install should have left is missing: a failed check too.
    except (CheckFailed, OSError) as failure:
        raise CheckFailed(f"{title}: {failure}") from None
    print(f"=== ok: {title}", flush=True)


def kalinka_releases(feed):
    """The feed's app-bundle releases, newest first, as install-release.sh picks."""
    return [
        release
        for release in feed
        if not release.get("draft")
        and not release.get("prerelease")
        and str(release.get("tag_name", "")).startswith("kalinka-v")
    ]


def bundle(release):
    """{package: version} of the release's _all debs, all of which Debian 13 gets."""
    packages = {}
    for asset in release.get("assets", []):
        name = asset["name"]
        if name.endswith("_all.deb"):
            package, version, _ = name.split("_")
            packages[package] = version
    return packages


def github(url):
    """The JSON GitHub's API answers *url* with, or CheckFailed."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"}
    )
    if os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise CheckFailed(f"could not read {url}: {error}") from None


def quota_wait(core, needed, now):
    """Seconds until the API quota *core* covers *needed* calls: 0 when it does now."""
    if core["remaining"] >= needed:
        return 0
    return max(0, core["reset"] - now) + RESET_SLACK


def wait_for_api_quota(needed):
    """Waits, saying so, until GitHub's API lets this address make *needed* calls.

    Without a token that is 60 an hour, which a few runs of the suite in a row
    use up; a run that started short would fail on the installer's lookups.
    """
    deadline = time.monotonic() + QUOTA_TIMEOUT
    while True:
        try:
            core = github(RATE_LIMIT_URL)["resources"]["core"]
        except (KeyError, TypeError) as error:
            raise CheckFailed(f"{RATE_LIMIT_URL} has no core quota: {error}") from None
        print(
            f"GitHub API: {core['remaining']} of {core['limit']} calls left", flush=True
        )
        delay = quota_wait(core, needed, time.time())
        if not delay:
            return
        check(
            time.monotonic() + delay < deadline,
            f"GitHub's API quota stayed short of {needed} calls for {QUOTA_TIMEOUT} s",
        )
        print(
            f"waiting {delay:.0f} s for it to reset; GITHUB_TOKEN raises it", flush=True
        )
        time.sleep(delay)


def published_releases():
    wait_for_api_quota(API_CALLS)
    releases = kalinka_releases(github(RELEASES_URL))
    check(releases, f"no published kalinka-v* release in {REPO}")
    return releases


def unit_settings(text):
    """A unit file's [Service] settings, each name to its values in order."""
    settings = {}
    section = None
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line:
            name, value = line.split("=", 1)
            settings.setdefault(name.strip(), []).append(value.strip())
    return settings


class Service:
    """A service's commands, run as its unit says, on a machine where no systemd runs.

    Covers what kalinka.service uses: User and Group, WorkingDirectory,
    Environment, the directories systemd makes for it, ExecStartPre (``+``
    runs one as root) and ExecStart. The unit's sandboxing is not applied.
    """

    DIRECTORY_ROOTS = {
        "StateDirectory": "/var/lib",
        "LogsDirectory": "/var/log",
        "RuntimeDirectory": "/run",
        "CacheDirectory": "/var/cache",
    }

    def __init__(self, unit_text):
        settings = unit_settings(unit_text)

        def last(name, default=None):
            return settings.get(name, [default])[-1]

        self.user = last("User")
        self.group = last("Group", self.user)
        self.working_directory = last("WorkingDirectory", "/")
        self.environment = {}
        for line in settings.get("Environment", []):
            for assignment in shlex.split(line):
                name, value = assignment.split("=", 1)
                self.environment[name] = value
        self.directories = [
            (Path(root, name), int(last(f"{key}Mode", "0755"), 8))
            for key, root in self.DIRECTORY_ROOTS.items()
            for line in settings.get(key, [])
            for name in line.split()
        ]
        self.pre = [
            (command.startswith("+"), shlex.split(command.removeprefix("+")))
            for command in settings.get("ExecStartPre", [])
        ]
        self.start = shlex.split(last("ExecStart"))

    def run(self, output):
        """Runs ExecStartPre to the end, then starts ExecStart writing to *output*.

        The started process leads its own process group, for stop() to end.
        """
        account = pwd.getpwnam(self.user)
        gid = grp.getgrnam(self.group).gr_gid
        for path, mode in self.directories:
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, account.pw_uid, gid)
            path.chmod(mode)
        environment = {
            "PATH": SYSTEM_PATH,
            "HOME": account.pw_dir,
            "USER": self.user,
            "LOGNAME": self.user,
            "SHELL": account.pw_shell,
            **self.environment,
        }
        as_user = {
            "user": account.pw_uid,
            "group": gid,
            "extra_groups": os.getgrouplist(self.user, gid),
        }
        for as_root, command in self.pre:
            try:
                subprocess.run(
                    command,
                    cwd=self.working_directory,
                    env=environment,
                    check=True,
                    timeout=BOOTSTRAP_TIMEOUT,
                    **({} if as_root else as_user),
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise CheckFailed(f"ExecStartPre: {error}") from None
        return subprocess.Popen(
            self.start,
            cwd=self.working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            **as_user,
        )


def wait_for_answer(process, url, timeout, poll=2.0):
    """The JSON *url* answers with; CheckFailed if *process* exits or time runs out."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.load(response)
        except (OSError, ValueError):
            pass
        if process.poll() is not None:
            raise CheckFailed(
                f"it exited with status {process.returncode} before {url} answered"
            )
        if time.monotonic() >= deadline:
            raise CheckFailed(f"{url} did not answer within {timeout} s")
        time.sleep(poll)


def stop(process, grace=30):
    """Ends the process group *process* leads: SIGTERM, and SIGKILL after *grace* s.

    Whatever of the group outlives its leader is killed too, so nothing it
    started holds the port into the next step.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(grace)
    _kill_group(process)
    process.wait()


def install(*args, piped=False, **env):
    """install-release.sh's output, or CheckFailed when it fails."""
    status, output = run_installer(*args, piped=piped, **env)
    check(status == 0, f"install-release.sh exited with status {status}")
    return output


def run_installer(*args, piped=False, **env):
    """install-release.sh's exit status and output; CheckFailed if it runs too long.

    *piped* feeds the script to ``bash -s`` through a pipe, as ``curl … | bash``
    does.
    """
    SYSTEMCTL_CALLS.unlink(missing_ok=True)
    if piped:
        command = ["bash", "-s", "--", *args]
    else:
        command = ["bash", str(INSTALLER), *args]
    print("$", *(f"{k}={v}" for k, v in env.items()), *command, flush=True)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env={**os.environ, "PATH": SYSTEM_PATH, **env},
        # Not process_group, which old-python's 3.10 lacks: a new session leads a group too.
        start_new_session=True,
    )
    if piped:
        threading.Thread(target=_feed, args=(process.stdin,), daemon=True).start()
    timer = threading.Timer(INSTALL_TIMEOUT, _kill_group, (process,))
    timer.start()
    lines = []
    try:
        with process.stdout:
            for line in process.stdout:
                print(line, end="", flush=True)
                lines.append(line)
        process.wait()
    finally:
        timer.cancel()
    check(
        process.returncode != -signal.SIGKILL,
        f"install-release.sh was stopped after {INSTALL_TIMEOUT} s",
    )
    return process.returncode, "".join(lines)


def _feed(pipe):
    with pipe:
        pipe.write(INSTALLER.read_text())


def _kill_group(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def package_state(package):
    """dpkg's status and version of *package*, "installed 5.18.0" say.

    "unknown to dpkg" when dpkg has no record of it.
    """
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown to dpkg"


def check_installed(release, output, *, web, renderer):
    tag = release["tag_name"]
    check(DONE.format(tag=tag) in output, f"the script did not report {tag} installed")
    for package, version in sorted(bundle(release).items()):
        state = package_state(package)
        check(
            state == f"installed {version}",
            f"{package} is {state}, not installed {version}",
        )
    for package, wanted in (("kalinka-web", web), ("kalinka-renderer", renderer)):
        state = package_state(package)
        check(
            state.startswith("installed ") == wanted,
            f"{package} is {state}, but should {'' if wanted else 'not '}be installed",
        )
    check(MISSING_EXTRA not in output, "a package for an optional feature did not come")
    check(MISSING_DEB not in output, "a downloaded package did not install")
    check(shutil.which("fpcalc"), "no fpcalc came for AcoustID")
    audit = subprocess.run(["dpkg", "--audit"], capture_output=True, text=True)
    check(not audit.stdout.strip(), f"dpkg --audit finds:\n{audit.stdout}")


def check_started(*units):
    """Each unit's postinst asked for it to be restarted, as a real system would."""
    calls = SYSTEMCTL_CALLS.read_text().splitlines() if SYSTEMCTL_CALLS.exists() else []
    for unit in units:
        check(f"restart {unit}" in calls, f"no postinst asked to restart {unit}")


def check_server_answers(release):
    expected = bundle(release)["kalinka-server"]
    service = Service(SERVER_UNIT.read_text())
    with tempfile.TemporaryFile("w+", errors="replace") as output:
        process = service.run(output)
        try:
            answer = wait_for_answer(process, VERSION_URL, ANSWER_TIMEOUT)
        except CheckFailed as failure:
            output.seek(0)
            tail = "".join(output.readlines()[-80:])
            raise CheckFailed(
                f"the server, started as {SERVER_UNIT.name} starts it: {failure}\n"
                f"{tail}"
            ) from None
        finally:
            stop(process)
    print(f"server answered: {json.dumps(answer)[:200]}", flush=True)
    check(
        answer.get("server_version") == expected,
        f"/server/version reports {answer.get('server_version')!r}, not {expected}",
    )


def fresh_debian():
    releases = published_releases()
    check(len(releases) >= 2, "an upgrade needs two published kalinka-v* releases")
    latest, previous = releases[0], releases[1]
    with step(f"install {previous['tag_name']}, asked for by its version"):
        output = install(previous["tag_name"].removeprefix("kalinka-v"))
        check_installed(previous, output, web=True, renderer=True)
        check_started("kalinka.service", "kalinka-renderer.service")
        check_server_answers(previous)
        server_id = SERVER_ID.read_text()
    with step(f"run it again, which upgrades it in place to {latest['tag_name']}"):
        output = install()
        check_installed(latest, output, web=True, renderer=True)
        check_started("kalinka.service")
        check(SERVER_ID.read_text() == server_id, "the upgrade changed the server id")
        check_server_answers(latest)


def dietpi_like():
    latest = published_releases()[0]
    with step(
        f"install {latest['tag_name']} with KALINKA_RENDERER=0 KALINKA_WEB=0, "
        "apt's install of the downloaded packages failing"
    ):
        fired = Path(f"{APT_GET}.fired")
        shutil.copy(HERE / "apt-get-failing-once", APT_GET)
        APT_GET.chmod(0o755)
        try:
            output = install(KALINKA_RENDERER="0", KALINKA_WEB="0")
            check(fired.exists(), "apt-get never got the downloaded packages to fail")
        finally:
            APT_GET.unlink()
            fired.unlink(missing_ok=True)
        check(FALLBACK in output, "the failed install was not followed by dpkg's")
        check(RENDERER_STEP not in output, "KALINKA_RENDERER=0 still ran the renderer")
        check(APP_LOOKUP not in output, "KALINKA_WEB=0 still looked the web player up")
        check_installed(latest, output, web=False, renderer=False)
        check_started("kalinka.service")
        check_server_answers(latest)
    with step("run it again piped into bash, with the renderer installer unreachable"):
        output = install(piped=True, KALINKA_RENDERER_INSTALLER=UNREACHABLE_INSTALLER)
        check(RENDERER_WARNING in output, "no warning that the renderer did not come")
        check_installed(latest, output, web=True, renderer=False)
    with step(
        f"run it again where apt can never install the server's {BLOCKED_DEPENDENCY},"
        " as on too old a Python: it has to fail"
    ):
        removal = subprocess.run(
            ["apt-get", "remove", "-y", "-q", BLOCKED_DEPENDENCY],
            stdin=subprocess.DEVNULL,
        )
        check(removal.returncode == 0, f"removing {BLOCKED_DEPENDENCY} failed")
        state = package_state("kalinka-server")
        check(
            not state.startswith("installed "),
            f"kalinka-server is still {state} without {BLOCKED_DEPENDENCY}",
        )
        BLOCK.write_text(
            f"Package: {BLOCKED_DEPENDENCY}\nPin: version *\nPin-Priority: -1\n"
        )
        try:
            status, output = run_installer(KALINKA_RENDERER="0", KALINKA_WEB="0")
        finally:
            BLOCK.unlink()
        check(status != 0, "install-release.sh exited with status 0")
        check(FALLBACK in output, "the failed install was not followed by dpkg's")
        check(SAID_DONE not in output, "it said it was done")
        check(
            SERVER_NOT_INSTALLED.format(tag=latest["tag_name"]) in output,
            "it did not say that the server is not installed",
        )
        state = package_state("kalinka-server")
        check(not state.startswith("installed "), f"kalinka-server is {state}")


def old_python():
    try:
        system = platform.freedesktop_os_release().get("PRETTY_NAME") or "this system"
    except OSError:
        system = "this system"
    python = "%d.%d.%d" % sys.version_info[:3]
    with step(f"refuse to install on {system}, whose Python is {python}"):
        status, output = run_installer()
        check(status != 0, "install-release.sh exited with status 0")
        check(
            OLD_PYTHON.format(system=system, python=python) in output,
            "it did not say that the server needs a newer Python than this one",
        )
        check(SUPPORTED in output, "it did not name the systems it installs on")
        check(RELEASE_LOOKUP not in output, "it looked a release up before it refused")
        state = package_state("kalinka-server")
        check(state == "unknown to dpkg", f"kalinka-server is {state}")


SYSTEMS = {"debian": fresh_debian, "dietpi": dietpi_like, "old-python": old_python}


def main(argv):
    if len(argv) != 2 or argv[1] not in SYSTEMS:
        print(f"usage: cases.py {'|'.join(SYSTEMS)}", file=sys.stderr)
        return 2
    try:
        SYSTEMS[argv[1]]()
    except CheckFailed as failure:
        print(f"\nFAIL {failure}", flush=True)
        return 1
    print(f"\nPASS {argv[1]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
