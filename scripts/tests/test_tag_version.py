"""Version a build of a tag-released train from the tags of its own checkout."""
from datetime import date
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'tag_version.sh'


class TagVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / 'scripts').mkdir()
        shutil.copy(HELPER, self.repo / 'scripts')
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        self.git('add', '.')
        self.commit()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git('commit', '-q', '--allow-empty', '-m', 'change')

    def version(self, prefix='kalinka-supervisor-v'):
        return subprocess.run(['bash', '-c', '. scripts/tag_version.sh && tag_version "$1"', '_', prefix],
                              cwd=self.repo, capture_output=True, text=True)

    def test_a_tagged_clean_tree_is_the_release(self):
        self.git('tag', 'kalinka-supervisor-v0.2.0')
        self.assertEqual(self.version().stdout.strip(), '0.2.0')

    def test_commits_past_a_tag_lead_up_to_the_next_patch(self):
        self.git('tag', 'kalinka-supervisor-v0.2.0')
        self.commit()
        self.commit()
        self.assertEqual(self.version().stdout.strip(), f'0.2.1~dev2+g{self.git("rev-parse", "--short", "HEAD")}')

    def test_uncommitted_changes_carry_the_date(self):
        self.git('tag', 'kalinka-supervisor-v0.2.0')
        (self.repo / 'scratch').write_text('x')
        self.assertTrue(self.version().stdout.strip().endswith(f'.d{date.today():%y%m%d}'))

    def test_only_the_trains_own_tags_count(self):
        self.git('tag', 'kalinka-supervisor-v0.1.0')
        self.commit()
        self.git('tag', 'kalinka-renderer-v0.7.0')
        self.assertTrue(self.version().stdout.startswith('0.1.1~dev1+g'))
        self.assertEqual(self.version('kalinka-renderer-v').stdout.strip(), '0.7.0')

    def test_no_tag_fails_instead_of_inventing_a_version(self):
        result = self.version()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('kalinka-supervisor-v', result.stderr)

    def test_a_tag_that_is_not_major_minor_patch_fails(self):
        self.git('tag', 'kalinka-supervisor-v0.2.0-rc1')
        self.commit()
        result = self.version()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_outside_a_checkout_fails(self):
        shutil.rmtree(self.repo / '.git')
        self.assertNotEqual(self.version().returncode, 0)

    @unittest.skipIf(shutil.which('dpkg') is None, 'needs dpkg')
    def test_dpkg_installs_each_build_over_the_one_before(self):
        self.git('tag', 'kalinka-supervisor-v0.2.0')
        builds = [self.version().stdout.strip()]
        for _ in range(2):
            self.commit()
            builds.append(self.version().stdout.strip())
        builds.append('0.2.1')
        for older, newer in zip(builds, builds[1:]):
            with self.subTest(older=older, newer=newer):
                subprocess.run(['dpkg', '--compare-versions', older, 'lt', newer], check=True)


if __name__ == '__main__':
    unittest.main()
