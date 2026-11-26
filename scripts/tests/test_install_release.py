"""Run the bundle installer with local release fixtures and no system changes."""

import json
import os
import pty
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install-release.sh"


class InstallerHarness(unittest.TestCase):
    """The installer against fake curl, apt and dpkg, and a release fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            FIXTURE=str(self.root),
            KALINKA_REPO="Kalinka-Player/KalinkaPlayer",
            KALINKA_WEB_REPO="Kalinka-Player/KalinkaAI",
            KALINKA_WEB="1",
            KALINKA_DISPLAY="0",
            KALINKA_RENDERER="0",
            KALINKA_REINSTALL="0",
            SKIP_SUPERVISOR="1",
            FAKE_ARCH="amd64",
            FAKE_MACHINE="x86_64",
            SUPERVISOR_FAILS="0",
            NO_APT_UPDATE="1",
            GITHUB_TOKEN="",
            FAIL_BUNDLE="0",
            DPKG_LOCKED="0",
            REPAIR_REMOVES="",
            REPAIR_FAILS="0",
        )
        self.installed = {}
        self.installer = self.root / "install-release.sh"
        # Only redirect the OS identity file and the system's Python, which is
        # the real one unless a test stands one in; run the full installer unchanged.
        self.installer.write_text(
            INSTALLER.read_text()
            .replace("/etc/os-release", str(self.root / "os-release"))
            .replace("/proc/device-tree/model", str(self.root / "model"))
            .replace("/usr/bin/python3", str(self.bin / "python3"))
        )
        self.platform("debian", "13")
        self.command("id", "#!/bin/sh\necho 0\n")
        self.command(
            "uname",
            '#!/bin/sh\n[ "$1" = -m ] && exec echo "$FAKE_MACHINE"\n'
            'exec /usr/bin/uname "$@"\n',
        )
        self.command(
            "dpkg-query",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
status = pathlib.Path(os.environ["FIXTURE"]) / "status.json"
state = json.loads(status.read_text()).get(sys.argv[-1])
formats = ("-f=${db:Status-Status}", "-f=${db:Status-Status} ${Version}")
if state is None or sys.argv[1] != "-W" or sys.argv[2] not in formats:
    sys.exit(1)
print(state if sys.argv[2] == formats[1] else state.split()[0], end="")
""",
        )
        self.command(
            "curl",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FIXTURE"])
args = sys.argv[1:]
url = next(a for a in args if a.startswith("https://"))
with (root / "requests.jsonl").open("a") as log:
    log.write(json.dumps(url) + "\\n")
if "api.github.com" in url:
    if "/KalinkaAI/" in url:
        data = json.loads((root / "app.json").read_text())
    else:
        data = json.loads((root / "release.json").read_text())
        if "/tags/" in url:
            data = next(r for r in data if r["tag_name"] == url.rsplit("/", 1)[1])
    print(json.dumps(data))
else:
    pathlib.Path(args[args.index("-o") + 1]).write_text(url)
""",
        )
        self.command(
            "apt-get",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FIXTURE"])
args = sys.argv[1:]
with (root / "apt.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if "-s" in args:
    sys.exit(1)  # Optional OS packages are outside this fixture.
packages = [pathlib.Path(a) for a in args if a.endswith(".deb")]
assert all(p.is_file() for p in packages), packages
if packages and os.environ["FAIL_BUNDLE"] == "1":
    sys.exit(1)
if "-f" in args and os.environ["REPAIR_FAILS"] == "1":
    sys.exit(100)
status = root / "status.json"
states = json.loads(status.read_text())
for package, version, _ in (p.name.split("_") for p in packages):
    states[package] = f"installed {version}"
if "-f" in args:
    # What dpkg -i left unconfigured is configured, or removed where it cannot be.
    removed = os.environ["REPAIR_REMOVES"].split()
    for package, state in list(states.items()):
        status_word, version = state.split()
        if status_word == "unpacked":
            fixed = "config-files" if package in removed else "installed"
            states[package] = f"{fixed} {version}"
status.write_text(json.dumps(states))
""",
        )
        self.command(
            "dpkg",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1:] == ["--print-architecture"]:
    print(os.environ["FAKE_ARCH"])
    sys.exit(0)
root = pathlib.Path(os.environ["FIXTURE"])
with (root / "dpkg.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ["DPKG_LOCKED"] == "1":
    sys.exit(2)
status = root / "status.json"
states = json.loads(status.read_text())
debs = [pathlib.Path(a).name for a in sys.argv[1:] if a.endswith(".deb")]
for package, version, _ in (name.split("_") for name in debs):
    states[package] = f"unpacked {version}"
status.write_text(json.dumps(states))
""",
        )
        self.base_names = [
            "kalinka-server_5.17.0_all.deb",
            "kalinka-plugin-sdk_3.21.0_all.deb",
            "kalinka-plugin-localfiles_5.17.0_all.deb",
        ]
        self.bluetooth = "kalinka-plugin-bluetooth_5.17.0_all.deb"
        self.web = "kalinka-web_0.25.0_all.deb"
        self.release = self.asset_release(
            "kalinka-v5.17.0", self.base_names + [self.bluetooth]
        )
        self.feed()
        (self.root / "app.json").write_text(
            json.dumps([self.asset_release("app-v0.25.0", [self.web])])
        )

    def command(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def platform(self, distro, version):
        (self.root / "os-release").write_text(
            f'ID={distro}\nVERSION_ID="{version}"\nID_LIKE=debian\n'
        )

    @staticmethod
    def asset_release(tag, names):
        return {
            "tag_name": tag,
            "assets": [
                {"name": name, "browser_download_url": f"https://example.test/{name}"}
                for name in names
            ],
        }

    def feed(self):
        (self.root / "release.json").write_text(
            json.dumps(
                [
                    {"tag_name": "kalinka-v99.0.0", "draft": True},
                    {"tag_name": "kalinka-v98.0.0", "prerelease": True},
                    {"tag_name": "kalinka-renderer-v0.13.0"},
                    self.release,
                ]
            )
        )

    def calls(self, name):
        log = self.root / f"{name}.jsonl"
        return (
            [json.loads(line) for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )

    def python(self, *version, directory=None):
        """python3 is the real one, but says it is *version* to the code it runs.

        It stands in for the system's Python unless *directory* puts it elsewhere.
        """
        path = (directory or self.bin) / "python3"
        path.write_text(
            f"""#!{sys.executable}
import runpy, sys
sys.version_info = {(*version, "final", 0)!r}
if sys.argv[1] == "-c":
    code, sys.argv = sys.argv[2], ["-c", *sys.argv[3:]]
    exec(code, {{"__name__": "__main__"}})
else:
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
""",
        )
        path.chmod(0o755)

    def run_installer(self, *args, piped=False):
        for log in self.root.glob("*.jsonl"):
            log.unlink()
        (self.root / "status.json").write_text(json.dumps(self.installed))
        return subprocess.run(
            ["bash", "-s", "--", *args]
            if piped
            else ["bash", str(self.installer), *args],
            input=self.installer.read_text() if piped else None,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def assert_packages(self, names):
        downloaded = {
            url.rsplit("/", 1)[1]
            for url in self.calls("requests")
            if url.endswith(".deb")
        }
        installed = {
            Path(a).name
            for call in self.calls("apt")
            for a in call
            if a.endswith(".deb")
        }
        self.assertEqual(downloaded, set(names))
        self.assertEqual(installed, set(names))


class ReleaseInstallerTests(InstallerHarness):
    def test_the_whole_bundle_is_downloaded_and_installed(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])
        self.assertIn("kalinka-v5.17.0 installed", result.stdout)
        self.assertNotIn("did not install", result.stderr)
        self.assertEqual(self.calls("dpkg"), [])

    def test_old_bundles_work_with_latest_and_each_supported_version_spelling(self):
        self.release = self.asset_release("kalinka-v5.17.0", self.base_names)
        self.feed()
        for args in [(), ("5.17.0",), ("v5.17.0",), ("kalinka-v5.17.0",)]:
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_packages(self.base_names + [self.web])

    def test_an_explicit_version_installs_its_whole_bundle(self):
        result = self.run_installer("5.17.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])

    def test_piped_installer_supports_current_and_old_bundles(self):
        for names in [self.base_names + [self.bluetooth], self.base_names]:
            with self.subTest(names=names):
                self.release = self.asset_release("kalinka-v5.17.0", names)
                self.feed()
                result = self.run_installer("5.17.0", piped=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_packages(names + [self.web])

    def test_the_repair_fallback_installs_the_bundle_it_downloaded(self):
        self.env["FAIL_BUNDLE"] = "1"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.base_names + [self.bluetooth, self.web]
        self.assert_packages(names)
        self.assertEqual(
            {
                Path(a).name
                for call in self.calls("dpkg")
                for a in call
                if a.endswith(".deb")
            },
            set(names),
        )
        self.assertTrue(any("-f" in call for call in self.calls("apt")))
        self.assertNotIn("did not install", result.stderr)

    def test_a_repair_that_removes_the_server_fails_the_install(self):
        self.env.update(
            FAIL_BUNDLE="1", REPAIR_REMOVES="kalinka-server kalinka-plugin-localfiles"
        )
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(any("-f" in call for call in self.calls("apt")))
        self.assertIn(
            "error: kalinka-v5.17.0 is not installed: dpkg reports kalinka-server "
            "as 'config-files 5.17.0', not 'installed 5.17.0'.",
            result.stderr,
        )
        self.assertNotIn(">> Done", result.stdout)

    def test_a_repair_that_fails_fails_the_install_and_says_so(self):
        self.env.update(FAIL_BUNDLE="1", REPAIR_FAILS="1")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "error: kalinka-v5.17.0 is not installed: apt could not repair the failed "
            "install. apt's output above says why.",
            result.stderr,
        )
        self.assertNotIn(">> Done", result.stdout)

    def test_an_upgrade_that_leaves_the_previous_server_fails_the_install(self):
        """dpkg -i waits for no lock, and a repair with nothing unpacked succeeds."""
        self.installed = {
            "kalinka-server": "installed 5.16.0",
            "kalinka-plugin-sdk": "installed 3.20.0",
        }
        self.env.update(FAIL_BUNDLE="1", DPKG_LOCKED="1")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(any("-f" in call for call in self.calls("apt")))
        self.assertIn(
            "error: kalinka-v5.17.0 is not installed: dpkg reports kalinka-server "
            "as 'installed 5.16.0', not 'installed 5.17.0'.",
            result.stderr,
        )
        self.assertNotIn(">> Done", result.stdout)

    def test_another_package_the_repair_removes_is_named_and_the_server_runs(self):
        """A system with the server's Python may lack the BlueZ of the Bluetooth plugin."""
        self.env.update(FAIL_BUNDLE="1", REPAIR_REMOVES="kalinka-plugin-bluetooth")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(">> Done. kalinka-v5.17.0 installed.", result.stdout)
        notes = [
            line for line in result.stderr.splitlines() if "did not install" in line
        ]
        self.assertEqual(
            notes,
            [
                ">> note: kalinka-plugin-bluetooth did not install: dpkg reports it "
                "as 'config-files 5.17.0', not 'installed 5.17.0'."
            ],
        )
        self.assertIn("The server runs without what is missing", result.stderr)

    def test_a_package_left_at_its_older_version_is_named(self):
        self.installed = {
            name.split("_")[0]: f"installed {name.split('_')[1]}"
            for name in self.base_names
        }
        self.installed["kalinka-web"] = "installed 0.24.0"
        self.env.update(FAIL_BUNDLE="1", DPKG_LOCKED="1")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            ">> note: kalinka-web did not install: dpkg reports it as "
            "'installed 0.24.0', not 'installed 0.25.0'.",
            result.stderr,
        )

    def test_a_python_older_than_the_servers_is_refused_before_any_lookup(self):
        for name, version in [
            ("Debian GNU/Linux 11 (bullseye)", (3, 9, 2)),
            ("Ubuntu 22.04.5 LTS", (3, 10, 12)),
        ]:
            with self.subTest(name=name):
                (self.root / "os-release").write_text(f'PRETTY_NAME="{name}"\n')
                self.python(*version)
                result = self.run_installer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(
                    "error: Kalinka needs Python 3.11 or newer, and "
                    f"{name} has Python {'.'.join(map(str, version))}.\n"
                    "  It installs on Debian 13, Raspberry Pi OS (64-bit) or DietPi "
                    "on Debian 13, or Ubuntu 26.04:\n"
                    "  https://github.com/Kalinka-Player/KalinkaPlayer/blob/main/"
                    "docs/installation.md#d-install-with-one-command\n",
                    result.stderr,
                )
                self.assertNotIn(">> Looking up", result.stdout)
                self.assertEqual(self.calls("requests"), [])
                self.assertEqual(self.calls("apt"), [])

    def test_an_unnamed_system_is_refused_by_its_python_without_an_inherited_name(
        self,
    ):
        (self.root / "os-release").unlink()
        self.env.update(PRETTY_NAME="Ubuntu 24.04.3 LTS")
        self.python(3, 10, 12)
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("and this system has Python 3.10.12.", result.stderr)
        self.assertEqual(self.calls("requests"), [])

    def test_a_python_that_does_not_run_is_not_called_too_old(self):
        self.command("python3", "#!/bin/sh\nexit 1\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            f"error: {self.bin / 'python3'} does not run, and the server needs it",
            result.stderr,
        )
        self.assertNotIn("Python 3.11 or newer", result.stderr)
        self.assertEqual(self.calls("requests"), [])

    def test_python_311_installs(self):
        self.python(3, 11, 2)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])
        self.assertIn("kalinka-v5.17.0 installed", result.stdout)

    def test_an_older_python_first_on_path_is_not_the_systems(self):
        """A virtual environment or pyenv on PATH is not what apt installs against."""
        self.platform("debian", "13")
        self.python(3, 13, 5)
        user_bin = self.root / "user-bin"
        user_bin.mkdir()
        self.python(3, 10, 12, directory=user_bin)
        self.env["PATH"] = f"{user_bin}:{self.env['PATH']}"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])

    def test_a_system_without_os_release_still_installs(self):
        (self.root / "os-release").unlink()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])

    def test_still_rejects_unsupported_bundles_without_the_all_server(self):
        self.release = self.asset_release(
            "kalinka-v5.17.0", ["kalinka-server_5.17.0_amd64.deb", self.bluetooth]
        )
        self.feed()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no arch-independent server package", result.stderr)
        self.assertEqual(self.calls("apt"), [])
        self.assertEqual(self.calls("dpkg"), [])

    def test_on_32_bit_arm_the_bundle_brings_the_libraries_its_wheels_need(self):
        self.env["FAKE_ARCH"] = "armhf"
        libs = "kalinka-server-libs_5.17.0_armhf.deb"
        self.release = self.asset_release(
            "kalinka-v5.17.0",
            self.base_names
            + [
                self.bluetooth,
                libs,
                "kalinka-kiosk_0.25.0_armhf.deb",
                "kalinka-other_5.17.0_arm64.deb",
            ],
        )
        self.feed()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, libs, self.web])

    def test_a_pi_1_is_pointed_to_the_renderer_installer_and_still_installed(self):
        self.env["FAKE_MACHINE"] = "armv6l"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install-renderer.sh", result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])

    def test_at_a_terminal_a_pi_1_has_time_to_stop_before_the_server_installs(self):
        self.env["FAKE_MACHINE"] = "armv6l"
        self.command("sleep", '#!/bin/sh\necho "$@" >> "$FIXTURE/sleep.log"\n')
        (self.root / "status.json").write_text(json.dumps(self.installed))
        terminal, stderr = pty.openpty()
        try:
            result = subprocess.run(
                ["bash", str(self.installer)], env=self.env, stdout=subprocess.DEVNULL,
                stderr=stderr, timeout=15, check=False,
            )
        finally:
            os.close(stderr)
            os.close(terminal)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.root / "sleep.log").read_text().split(), ["15"])

    def test_a_pi_1_already_running_the_server_upgrades_without_the_note(self):
        self.env["FAKE_MACHINE"] = "armv6l"
        self.installed["kalinka-server"] = "installed 5.16.0"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("install-renderer.sh", result.stderr)

    def test_other_machines_leave_the_32_bit_libraries_out(self):
        self.release = self.asset_release(
            "kalinka-v5.17.0",
            self.base_names + [self.bluetooth, "kalinka-server-libs_5.17.0_armhf.deb"],
        )
        self.feed()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])


class SupervisorTests(InstallerHarness):
    """Whether the bundle installer brings the supervisor, which a Raspberry Pi
    gets by default and any other machine on request."""

    def setUp(self):
        super().setUp()
        self.env.pop("SKIP_SUPERVISOR")
        (self.root / "install-supervisor.sh").write_text(
            '#!/usr/bin/env bash\necho ran >> "$FIXTURE/supervisor.log"\n'
            'exit "$SUPERVISOR_FAILS"\n'
        )

    def a_pi(self):
        (self.root / "model").write_bytes(b"Raspberry Pi Model B Plus Rev 1.2\0")

    def supervisor_ran(self):
        log = self.root / "supervisor.log"
        return log.exists() and log.read_text().count("ran") == 1

    def test_a_raspberry_pi_gets_the_supervisor(self):
        self.a_pi()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.supervisor_ran())
        self.assertIn(">> Installing the supervisor", result.stdout)

    def test_another_machine_gets_it_only_on_request(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.supervisor_ran())
        self.assertNotIn("No such file", result.stderr)

        self.env["KALINKA_SUPERVISOR"] = "1"
        self.run_installer()
        self.assertTrue(self.supervisor_ran())

    def test_a_pi_upgrading_a_server_without_it_still_goes_without(self):
        """The server's auto-upgrade reruns this script."""
        self.a_pi()
        self.installed["kalinka-server"] = "installed 0.15.0"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.supervisor_ran())

    def test_a_pi_can_opt_out(self):
        self.a_pi()
        self.env["KALINKA_SUPERVISOR"] = "0"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.supervisor_ran())

    def test_a_supervisor_that_cannot_be_installed_leaves_kalinka_installed(self):
        self.a_pi()
        self.env["SUPERVISOR_FAILS"] = "1"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the supervisor could not be installed", result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])

    def test_an_installed_supervisor_joins_the_upgrade_as_before(self):
        self.installed["kalinka-supervisor"] = "installed 0.2.0"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.supervisor_ran())

        self.env["SUPERVISOR_FAILS"] = "1"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("apt"), [])

    def test_the_renderer_installer_is_not_asked_for_it_again(self):
        self.a_pi()
        self.env["KALINKA_RENDERER"] = "1"
        (self.root / "install-renderer.sh").write_text(
            '#!/usr/bin/env bash\necho "$KALINKA_SUPERVISOR" > "$FIXTURE/renderer-supervisor"\n'
        )
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.supervisor_ran())
        self.assertEqual((self.root / "renderer-supervisor").read_text().strip(), "0")

    def test_the_image_build_still_skips_it(self):
        self.a_pi()
        self.env["SKIP_SUPERVISOR"] = "1"
        self.run_installer()
        self.assertFalse(self.supervisor_ran())


if __name__ == "__main__":
    unittest.main()
