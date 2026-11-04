"""Run the bundle installer with local release fixtures and no system changes."""

import json
import os
import subprocess
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
        self.command("dpkg-query", "#!/bin/sh\nexit 1\n")
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
""",
        )
        self.command(
            "dpkg",
            """#!/usr/bin/env python3
import json, os, pathlib, sys
with (pathlib.Path(os.environ["FIXTURE"]) / "dpkg.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
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

    def run_installer(self, *args, piped=False):
        for log in self.root.glob("*.jsonl"):
            log.unlink()
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
