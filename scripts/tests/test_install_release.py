"""Run the bundle installer with local release fixtures and no system changes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install-release.sh"


class ReleaseInstallerTests(unittest.TestCase):
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
            NO_APT_UPDATE="1",
            GITHUB_TOKEN="",
            FAIL_BUNDLE="0",
            REPAIR_REMOVES="0",
        )
        self.installer = self.root / "install-release.sh"
        # Only redirect the OS identity file; run the full installer unchanged.
        self.installer.write_text(
            INSTALLER.read_text().replace(
                "/etc/os-release", str(self.root / "os-release")
            )
        )
        self.platform("ubuntu", "24.04")
        self.command("id", "#!/bin/sh\necho 0\n")
        self.command(
            "dpkg-query",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
status = pathlib.Path(os.environ["FIXTURE"]) / "status.json"
state = json.loads(status.read_text()).get(sys.argv[-1]) if status.exists() else None
if state is None or sys.argv[1:3] != ["-W", "-f=${db:Status-Status}"]:
    sys.exit(1)
print(state, end="")
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
status = root / "status.json"
states = json.loads(status.read_text()) if status.exists() else {}
states.update((p.name.split("_")[0], "installed") for p in packages)
if "-f" in args:
    # What dpkg -i left unconfigured is configured, or removed where it cannot be.
    fixed = "config-files" if os.environ["REPAIR_REMOVES"] == "1" else "installed"
    states.update((name, fixed) for name, state in states.items() if state == "unpacked")
status.write_text(json.dumps(states))
""",
        )
        self.command(
            "dpkg",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FIXTURE"])
with (root / "dpkg.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
status = root / "status.json"
states = json.loads(status.read_text()) if status.exists() else {}
names = [pathlib.Path(a).name for a in sys.argv[1:] if a.endswith(".deb")]
states.update((name.split("_")[0], "unpacked") for name in names)
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

    def python(self, *version):
        """python3 is the real one, but says it is *version* to the code it runs."""
        self.command(
            "python3",
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

    def run_installer(self, *args, piped=False):
        for log in self.root.glob("*.jsonl"):
            log.unlink()
        (self.root / "status.json").unlink(missing_ok=True)
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

    def test_ubuntu_2404_excludes_bluetooth_before_download_and_install(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.web])
        self.assertIn("skipping the Bluetooth plugin on Ubuntu 24.04", result.stderr)
        self.assertIn("kalinka-v5.17.0 installed", result.stdout)
        self.assertEqual(self.calls("dpkg"), [])

    def test_other_platforms_keep_the_complete_bundle(self):
        for distro, version in [
            ("debian", "13"),
            ("raspbian", "13"),
            ("ubuntu", "22.04"),
            ("ubuntu", "26.04"),
        ]:
            with self.subTest(distro=distro, version=version):
                self.platform(distro, version)
                result = self.run_installer()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_packages(self.base_names + [self.bluetooth, self.web])
                self.assertNotIn("skipping the Bluetooth plugin", result.stderr)

    def test_old_bundles_work_with_latest_and_each_supported_version_spelling(self):
        self.release = self.asset_release("kalinka-v5.17.0", self.base_names)
        self.feed()
        for args in [(), ("5.17.0",), ("v5.17.0",), ("kalinka-v5.17.0",)]:
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_packages(self.base_names + [self.web])
                self.assertNotIn("skipping the Bluetooth plugin", result.stderr)

    def test_explicit_version_also_excludes_bluetooth(self):
        result = self.run_installer("5.17.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.web])

    def test_piped_installer_supports_current_and_old_bundles(self):
        for names in [self.base_names + [self.bluetooth], self.base_names]:
            with self.subTest(names=names):
                self.release = self.asset_release("kalinka-v5.17.0", names)
                self.feed()
                result = self.run_installer("5.17.0", piped=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_packages(self.base_names + [self.web])

    def test_repair_fallback_cannot_reintroduce_bluetooth(self):
        self.env["FAIL_BUNDLE"] = "1"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.base_names + [self.web]
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

    def test_a_repair_that_removes_the_server_fails_the_install(self):
        self.env.update(FAIL_BUNDLE="1", REPAIR_REMOVES="1")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(any("-f" in call for call in self.calls("apt")))
        self.assertIn(
            "error: kalinka-v5.17.0 is not installed: dpkg reports kalinka-server "
            "as 'config-files', not 'installed'.",
            result.stderr,
        )
        self.assertNotIn(">> Done", result.stdout)

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
                    "on Debian 13, or Ubuntu 24.04:\n"
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

    def test_python_311_installs(self):
        self.platform("debian", "12")
        self.python(3, 11, 2)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_packages(self.base_names + [self.bluetooth, self.web])
        self.assertIn("kalinka-v5.17.0 installed", result.stdout)

    def test_missing_os_release_preserves_selection_and_ignores_inherited_identity(
        self,
    ):
        (self.root / "os-release").unlink()
        self.env.update(ID="ubuntu", VERSION_ID="24.04")
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


if __name__ == "__main__":
    unittest.main()
