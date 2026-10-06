"""Tests for the update-check module behind GET /server/update."""

from __future__ import annotations

import asyncio
from datetime import datetime

from kalinka_server import update_check
from kalinka_server.update_check import (
    _BUNDLE_TAG_PREFIX,
    _RENDERER_TAG_PREFIX,
    UpdateChecker,
    deb_is_newer,
    installed_renderer_version,
    is_newer,
    latest_release_version,
    upgrade_supported,
    validate_upgrade_request,
)


def _feed(*tags):
    """Minimal GitHub releases.atom document with the given tags, newest first."""
    entries = "".join(
        f"<entry><id>tag:github.com,2008:Repository/12345/{tag}</id>"
        f"<title>{tag}</title></entry>"
        for tag in tags
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>Release notes</title>{entries}</feed>"
    )


class _FakeRun:
    """Stands in for subprocess.run, recording the command it was given."""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        return self


class TestLatestReleaseVersion:
    def test_picks_first_matching_release(self):
        feed = _feed("kalinka-v3.3.0", "kalinka-v3.2.0")
        assert latest_release_version(feed, _BUNDLE_TAG_PREFIX) == "3.3.0"

    def test_skips_foreign_tags(self):
        feed = _feed("jamendo-ai-v2", "kalinka-v3.3.0")
        assert latest_release_version(feed, _BUNDLE_TAG_PREFIX) == "3.3.0"

    def test_renderer_and_bundle_trains_do_not_shadow_each_other(self):
        feed = _feed("kalinka-renderer-v0.2.0", "kalinka-v3.3.0")
        assert latest_release_version(feed, _BUNDLE_TAG_PREFIX) == "3.3.0"
        assert latest_release_version(feed, _RENDERER_TAG_PREFIX) == "0.2.0"

    def test_none_when_no_matching_release(self):
        foreign = _feed("jamendo-ai-v1")
        assert latest_release_version(foreign, _BUNDLE_TAG_PREFIX) is None
        assert latest_release_version(_feed(), _BUNDLE_TAG_PREFIX) is None

    def test_none_on_malformed_feed(self):
        assert latest_release_version("<html>not a feed", _BUNDLE_TAG_PREFIX) is None


class TestIsNewer:
    def test_newer(self):
        assert is_newer("3.3.0", "3.2.0")

    def test_equal_and_older(self):
        assert not is_newer("3.2.0", "3.2.0")
        assert not is_newer("3.1.0", "3.2.0")

    def test_release_beats_dev_build_of_same_version(self):
        assert is_newer("3.3.0", "3.3.0.dev5+g1234567")

    def test_unbuilt_source_tree_reads_as_older(self):
        assert is_newer("3.2.0", "0.0.0")

    def test_invalid_versions_are_not_newer(self):
        assert not is_newer("not-a-version", "3.2.0")
        assert not is_newer("3.3.0", "not-a-version")


class TestInstalledRendererVersion:
    def test_reports_the_installed_package_version(self, monkeypatch):
        run = _FakeRun(stdout="installed 0.2.0\n")
        monkeypatch.setattr(update_check.subprocess, "run", run)
        assert installed_renderer_version() == "0.2.0"
        assert run.commands[0][:2] == ["dpkg-query", "-W"]

    def test_none_when_the_package_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            update_check.subprocess, "run", _FakeRun(returncode=1)
        )
        assert installed_renderer_version() is None

    def test_none_when_a_known_package_has_no_version(self, monkeypatch):
        # dpkg-query succeeds with an empty Version for a purged package.
        monkeypatch.setattr(update_check.subprocess, "run", _FakeRun(stdout=""))
        assert installed_renderer_version() is None

    def test_removed_package_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(update_check.subprocess, "run", _FakeRun(stdout="config-files 0.2.0"))
        assert installed_renderer_version() is None

    def test_none_without_dpkg(self, monkeypatch):
        def missing(*args, **kwargs):
            raise FileNotFoundError("dpkg-query")

        monkeypatch.setattr(update_check.subprocess, "run", missing)
        assert installed_renderer_version() is None


class TestDebIsNewer:
    def test_asks_dpkg_and_reads_its_verdict(self, monkeypatch):
        run = _FakeRun(returncode=0)
        monkeypatch.setattr(update_check.subprocess, "run", run)
        assert deb_is_newer("0.2.0", "0.1.0")
        assert run.commands[0] == [
            "dpkg",
            "--compare-versions",
            "0.2.0",
            "gt",
            "0.1.0",
        ]

    def test_not_newer_when_dpkg_says_no(self, monkeypatch):
        monkeypatch.setattr(
            update_check.subprocess, "run", _FakeRun(returncode=1)
        )
        assert not deb_is_newer("0.1.0", "0.2.0")

    def test_not_newer_when_dpkg_cannot_be_run(self, monkeypatch):
        def missing(*args, **kwargs):
            raise FileNotFoundError("dpkg")

        monkeypatch.setattr(update_check.subprocess, "run", missing)
        assert not deb_is_newer("0.2.0", "0.1.0")


class TestUpgradeSupported:
    def test_supported_when_root_side_files_exist(self, tmp_path, monkeypatch):
        script = tmp_path / "install-release.sh"
        unit = tmp_path / "kalinka-upgrade.path"
        script.touch()
        unit.touch()
        monkeypatch.delenv("KALINKA_PREFIX", raising=False)
        monkeypatch.setattr(update_check, "_UPGRADE_SCRIPT", str(script))
        monkeypatch.setattr(update_check, "_UPGRADE_PATH_UNIT", str(unit))
        assert upgrade_supported()

    def test_unsupported_when_files_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KALINKA_PREFIX", raising=False)
        monkeypatch.setattr(
            update_check, "_UPGRADE_SCRIPT", str(tmp_path / "missing.sh")
        )
        monkeypatch.setattr(
            update_check, "_UPGRADE_PATH_UNIT", str(tmp_path / "missing.path")
        )
        assert not upgrade_supported()

    def test_unsupported_under_dev_prefix(self, tmp_path, monkeypatch):
        """A dev fakeroot has no systemd watching its run dir, so upgrade
        must read as unsupported even if a deb install exists on the box."""
        script = tmp_path / "install-release.sh"
        unit = tmp_path / "kalinka-upgrade.path"
        script.touch()
        unit.touch()
        monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
        monkeypatch.setattr(update_check, "_UPGRADE_SCRIPT", str(script))
        monkeypatch.setattr(update_check, "_UPGRADE_PATH_UNIT", str(unit))
        assert not upgrade_supported()


class TestValidateUpgradeRequest:
    def test_accepts_matching_available_update(self):
        assert validate_upgrade_request("3.3.0", "3.3.0", "3.2.0") is None

    def test_rejects_when_no_update_known(self):
        assert validate_upgrade_request("3.3.0", None, "3.2.0") is not None

    def test_rejects_after_upgrade_already_happened(self):
        # Server upgraded to 3.3.0; a retried request for 3.3.0 must not
        # fire the installer again.
        assert validate_upgrade_request("3.3.0", "3.3.0", "3.3.0") is not None

    def test_rejects_stale_target_version(self):
        # A newer release was published since the client saw the banner.
        assert validate_upgrade_request("3.3.0", "3.4.0", "3.2.0") is not None

    def test_accepts_current_bundle_when_only_the_renderer_is_behind(self):
        # One installer run covers both, so the client keeps echoing the
        # bundle version it was shown.
        assert (
            validate_upgrade_request("3.3.0", "3.3.0", "3.3.0", True) is None
        )

    def test_rejects_stale_target_even_with_a_behind_renderer(self):
        assert (
            validate_upgrade_request("3.2.0", "3.3.0", "3.3.0", True)
            is not None
        )


class TestMaybeAutoUpgrade:
    def _armed_checker(self, monkeypatch, latest="3.3.0", supported=True):
        """Checker with an available update; records request_upgrade calls."""
        checker = UpdateChecker()
        checker._latest = latest
        requests = []
        monkeypatch.setattr(
            update_check, "request_upgrade", lambda: requests.append(1)
        )
        monkeypatch.setattr(
            update_check, "upgrade_supported", lambda: supported
        )
        monkeypatch.setattr(update_check, "get_version", lambda: "3.2.0")
        return checker, requests

    def _attempt(self, checker, at, stopped=None, renderers_ready=None):
        async def probe():
            return stopped

        asyncio.run(
            checker.maybe_auto_upgrade(
                None if stopped is None else probe,
                renderers_ready,
                now=at,
            )
        )

    def test_fires_in_quiet_hours(self, monkeypatch):
        checker, requests = self._armed_checker(monkeypatch)
        self._attempt(checker, datetime(2026, 7, 27, 3, 30))
        assert requests == [1]

    def test_does_not_fire_outside_quiet_hours(self, monkeypatch):
        checker, requests = self._armed_checker(monkeypatch)
        self._attempt(checker, datetime(2026, 7, 27, 14, 0))
        self._attempt(checker, datetime(2026, 7, 27, 6, 0))
        assert requests == []

    def test_at_most_one_attempt_per_day(self, monkeypatch):
        checker, requests = self._armed_checker(monkeypatch)
        self._attempt(checker, datetime(2026, 7, 27, 3, 0))
        self._attempt(checker, datetime(2026, 7, 27, 4, 0))
        assert requests == [1]
        # A failed install leaves the server running; retry next night.
        self._attempt(checker, datetime(2026, 7, 28, 3, 0))
        assert requests == [1, 1]

    def test_does_not_fire_without_newer_release(self, monkeypatch):
        checker, requests = self._armed_checker(monkeypatch, latest=None)
        self._attempt(checker, datetime(2026, 7, 27, 3, 0))
        checker._latest = "3.2.0"  # equal to running version
        self._attempt(checker, datetime(2026, 7, 27, 4, 0))
        assert requests == []

    def test_fires_for_a_behind_renderer_alone(self, monkeypatch):
        # Bundle equal to the running version; only the renderer is behind.
        checker, requests = self._armed_checker(monkeypatch, latest="3.2.0")
        checker._renderer_stale = True
        self._attempt(checker, datetime(2026, 7, 27, 3, 0))
        assert requests == [1]

    def test_does_not_fire_when_unsupported(self, monkeypatch):
        checker, requests = self._armed_checker(monkeypatch, supported=False)
        self._attempt(checker, datetime(2026, 7, 27, 3, 0))
        assert requests == []

    def test_postponed_while_playing_then_fires_when_stopped(
        self, monkeypatch
    ):
        checker, requests = self._armed_checker(monkeypatch)
        self._attempt(checker, datetime(2026, 7, 27, 3, 0), stopped=False)
        assert requests == []
        # Playback stopping later in the same window still upgrades tonight.
        self._attempt(checker, datetime(2026, 7, 27, 4, 0), stopped=True)
        assert requests == [1]


    def test_renderers_are_brought_forward_before_the_server_moves(
        self, monkeypatch
    ):
        """A release can move the renderer protocol, and a renderer stranded on
        another machine is the expensive failure — so it goes first."""
        checker, requests = self._armed_checker(monkeypatch)
        calls = []

        async def not_yet():
            calls.append(1)
            return False

        self._attempt(
            checker, datetime(2026, 7, 27, 3, 30), renderers_ready=not_yet
        )
        assert calls == [1]
        assert requests == []

        # The tick after the renderers came back finds nothing left to do.
        async def ready():
            return True

        self._attempt(
            checker, datetime(2026, 7, 27, 4, 30), renderers_ready=ready
        )
        assert requests == [1]

    def test_a_failing_renderer_check_holds_the_server_where_it_is(
        self, monkeypatch
    ):
        checker, requests = self._armed_checker(monkeypatch)

        async def boom():
            raise RuntimeError("registry is having a moment")

        self._attempt(checker, datetime(2026, 7, 27, 3, 30), renderers_ready=boom)
        assert requests == []

class TestUpdateChecker:
    def _checker_with_fetches(self, results, monkeypatch, installed=None):
        checker = UpdateChecker()
        fetches = iter(results)

        async def fake_fetch():
            return next(fetches)

        checker._fetch = fake_fetch
        monkeypatch.setattr(
            update_check, "installed_renderer_version", lambda: installed
        )
        monkeypatch.setattr(update_check, "deb_is_newer", lambda a, b: True)
        return checker

    def test_starts_with_no_known_release(self):
        checker = UpdateChecker()
        assert checker.latest is None
        assert checker.latest_renderer is None
        assert checker.installed_renderer is None

    def test_check_updates_both_release_trains(self, monkeypatch):
        checker = self._checker_with_fetches(
            [_feed("kalinka-v3.3.0", "kalinka-renderer-v0.2.0")],
            monkeypatch,
            installed="0.1.0",
        )
        assert asyncio.run(checker.check_now()) == "3.3.0"
        assert checker.latest == "3.3.0"
        assert checker.latest_renderer == "0.2.0"
        assert checker.installed_renderer == "0.1.0"

    def test_failed_check_keeps_last_known_result(self, monkeypatch):
        # An available update must not vanish on a network blip.
        checker = self._checker_with_fetches(
            [_feed("kalinka-v3.3.0", "kalinka-renderer-v0.2.0"), None],
            monkeypatch,
        )

        async def run():
            await checker.check_now()
            assert await checker.check_now() is None

        asyncio.run(run())
        assert checker.latest == "3.3.0"
        assert checker.latest_renderer == "0.2.0"

    def test_a_feed_page_without_a_renderer_release_keeps_the_known_one(
        self, monkeypatch
    ):
        # Renderer releases are rare enough to fall off the single feed page.
        checker = self._checker_with_fetches(
            [
                _feed("kalinka-v3.3.0", "kalinka-renderer-v0.2.0"),
                _feed("kalinka-v3.4.0"),
            ],
            monkeypatch,
        )

        async def run():
            await checker.check_now()
            await checker.check_now()

        asyncio.run(run())
        assert checker.latest == "3.4.0"
        assert checker.latest_renderer == "0.2.0"


class TestRendererUpdateAvailable:
    def _checked(self, monkeypatch, installed, published, newer=True):
        """Checker after one check against the given local/published pair."""
        checker = UpdateChecker()

        async def fake_fetch():
            tags = ["kalinka-v3.3.0"]
            if published:
                tags.append(f"kalinka-renderer-v{published}")
            return _feed(*tags)

        checker._fetch = fake_fetch
        monkeypatch.setattr(
            update_check, "installed_renderer_version", lambda: installed
        )
        monkeypatch.setattr(update_check, "deb_is_newer", lambda a, b: newer)
        asyncio.run(checker.check_now())
        return checker

    def test_available_when_the_release_outranks_the_installed_package(
        self, monkeypatch
    ):
        checker = self._checked(monkeypatch, "0.1.0", "0.2.0")
        assert checker.renderer_update_available()

    def test_not_available_when_up_to_date(self, monkeypatch):
        checker = self._checked(monkeypatch, "0.2.0", "0.2.0", newer=False)
        assert not checker.renderer_update_available()

    def test_not_available_when_no_renderer_is_installed(self, monkeypatch):
        # Adding one is an install decision, not an upgrade.
        checker = self._checked(monkeypatch, None, "0.2.0")
        assert not checker.renderer_update_available()

    def test_not_available_when_no_renderer_release_is_known(self, monkeypatch):
        checker = self._checked(monkeypatch, "0.1.0", None)
        assert not checker.renderer_update_available()

    def test_not_available_before_the_first_check(self):
        assert not UpdateChecker().renderer_update_available()

    def test_update_available_covers_either_component(self, monkeypatch):
        checker = self._checked(monkeypatch, "0.1.0", "0.2.0")
        monkeypatch.setattr(update_check, "get_version", lambda: "3.3.0")
        assert not checker.bundle_update_available()
        assert checker.update_available()

class TestSupervisorUpdates:
    async def test_supervisor_only_update_is_offered(self, monkeypatch):
        checker = UpdateChecker()
        monkeypatch.setattr(update_check, 'get_version', lambda: '5.7.0')
        monkeypatch.setattr(update_check, 'installed_renderer_version', lambda: None)
        monkeypatch.setattr(update_check, 'installed_supervisor_version', lambda: '0.1.0')
        async def fetch():
            return _feed('kalinka-supervisor-v0.2.0', 'kalinka-v5.7.0')
        monkeypatch.setattr(checker, '_fetch', fetch)
        await checker.check_now()
        assert checker.update_available()
        assert checker.supervisor_update_available()
        assert checker.latest_supervisor == '0.2.0'
        assert checker.installed_supervisor == '0.1.0'
        assert validate_upgrade_request('5.7.0', checker.latest, '5.7.0', supervisor_stale=True) is None
        assert validate_upgrade_request('5.6.0', checker.latest, '5.7.0', supervisor_stale=True) is not None

    async def test_absent_supervisor_is_not_an_update(self, monkeypatch):
        checker = UpdateChecker()
        monkeypatch.setattr(update_check, 'installed_renderer_version', lambda: None)
        monkeypatch.setattr(update_check, 'installed_supervisor_version', lambda: None)
        async def fetch():
            return _feed('kalinka-supervisor-v0.2.0')
        monkeypatch.setattr(checker, '_fetch', fetch)
        await checker.check_now()
        assert not checker.supervisor_update_available()

    async def test_supervisor_release_survives_feed_rotation(self, monkeypatch):
        checker = UpdateChecker()
        monkeypatch.setattr(update_check, 'installed_renderer_version', lambda: None)
        monkeypatch.setattr(update_check, 'installed_supervisor_version', lambda: '0.1.0~ci.4')
        feeds = iter([_feed('kalinka-supervisor-v0.1.0'), _feed('kalinka-v5.7.0')])
        async def fetch():
            return next(feeds)
        monkeypatch.setattr(checker, '_fetch', fetch)
        await checker.check_now()
        await checker.check_now()
        assert checker.supervisor_update_available()
