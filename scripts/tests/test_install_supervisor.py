"""Exercise release selection and verification without contacting apt or GitHub."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

INSTALLER = Path(__file__).resolve().parents[1] / 'install-supervisor.sh'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', FIXTURE=str(self.root))
        self.command('curl', '''#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
url = next(a for a in args if a.startswith('https://'))
name = 'release.json' if 'api.github.com' in url else url.rsplit('/', 1)[1]
shutil.copyfile(pathlib.Path(os.environ['FIXTURE']) / name, args[args.index('-o') + 1])
''')
        self.command('id', '#!/bin/sh\necho 0\n')
        self.command('apt-get', '#!/bin/sh\nprintf "%s\\n" "$@" > "$FIXTURE/installed"\n')
        self.command('dpkg-query', '#!/bin/sh\nexit 1\n')
        self.arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
        self.name = f'kalinka-supervisor_0.2.0_{self.arch}.deb'
        package = self.root / 'package' / 'DEBIAN'
        package.mkdir(parents=True)
        (package / 'control').write_text(f'Package: kalinka-supervisor\nVersion: 0.2.0\nArchitecture: {self.arch}\nMaintainer: Test <test@example.com>\nDescription: fixture\n')
        subprocess.run(['dpkg-deb', '--build', str(package.parent), str(self.root / self.name)], check=True, capture_output=True)
        digest = hashlib.sha256((self.root / self.name).read_bytes()).hexdigest()
        (self.root / 'SHA256SUMS').write_text(f'{digest}  ./{self.name}\n')
        self.release = {'tag_name': 'kalinka-supervisor-v0.2.0', 'assets': [
            {'name': name, 'browser_download_url': f'https://example.com/{name}'}
            for name in (self.name, 'SHA256SUMS')]}
        self.feed()

    def command(self, name, code):
        file = self.bin / name
        file.write_text(code)
        file.chmod(0o755)

    def feed(self):
        (self.root / 'release.json').write_text(json.dumps([
            {'tag_name': 'kalinka-v9.0.0'},
            {'tag_name': 'kalinka-supervisor-v0.3.0', 'prerelease': True},
            self.release]))

    def run_installer(self):
        return subprocess.run(['bash', str(INSTALLER)], env=self.env, capture_output=True, text=True)

    def test_installs_verified_stable_supervisor(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.name, (self.root / 'installed').read_text())

    def test_unreleased_ci_package_does_not_block_core_upgrades(self):
        (self.root / 'release.json').write_text('[]')
        self.env['ALLOW_MISSING_RELEASE'] = '1'
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertFalse((self.root / 'installed').exists())

    def test_explicit_install_without_a_release_fails(self):
        (self.root / 'release.json').write_text('[]')
        self.assertNotEqual(self.run_installer().returncode, 0)

    def test_checksum_failure_never_calls_apt(self):
        (self.root / self.name).write_bytes(b'changed package')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse((self.root / 'installed').exists())

    def test_missing_architecture_never_calls_apt(self):
        self.release['assets'] = self.release['assets'][1:]
        self.feed()
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse((self.root / 'installed').exists())

    def test_does_not_downgrade(self):
        self.command('dpkg-query', '#!/bin/sh\necho installed 0.3.0\n')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'installed').exists())

    def test_rejects_mismatched_package_version(self):
        self.command('dpkg-deb', f'#!/bin/sh\ncase "$3" in Package) echo kalinka-supervisor;; Architecture) echo {self.arch};; Version) echo 9.0.0;; esac\n')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse((self.root / 'installed').exists())


if __name__ == '__main__':
    unittest.main()
