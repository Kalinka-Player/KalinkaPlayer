"""Telling the user a music folder is wrong, while they can still fix it.

Two kinds of wrong, answered differently. How a folder is written is the
user's mistake and refuses the save, because nothing about it will improve
by keeping it. Whether it answers right now is not: a NAS switched off
tonight is still the right folder to have configured, and the library holds
what it indexed under a root it cannot currently see.

The credentials are the trap here. A password the user has typed but not
saved must reach the probe that judges the share, and must not reach the
resolver playback reads.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from kalinka_plugin_sdk import IssueSeverity

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles
from kalinka_plugin_localfiles.storage import StorageResolver
from kalinka_plugin_localfiles.storage.base import FileStorage, RootStatus
from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.storage.smb import SmbCredentials, SmbStorage


class _Context:
    def __init__(self, config):
        self.config = config


@pytest.fixture
def plugin(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    made = KalinkaPluginLocalFiles()
    made._context = _Context(
        LocalFilesConfig(
            music_folders=[str(music)],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
    )
    return made, music


def _judge(plugin, folders=None, changed=frozenset({"music_folders"}), **fields):
    made, _music = plugin
    values = made._context.config.model_dump()
    if folders is not None:
        values["music_folders"] = folders
    values.update(fields)
    return asyncio.run(
        made.validate_config(LocalFilesConfig(**values), changed)
    )


class TestHowAFolderIsWritten:
    def test_a_folder_that_is_there_draws_nothing(self, plugin):
        assert _judge(plugin) == []

    @pytest.mark.parametrize(
        "folder, said",
        [
            ("smb://nas", "name the share"),
            ("smb:/nas/music", "needs two slashes"),
            (r"\\nas\music", "not a Windows path"),
            ("ftp://nas/music", "not a protocol this module can read"),
            ("smb://user:secret@nas/music", "password does not belong"),
            ("smb://nas/../music", "does not belong in a share path"),
            ("", "the music folder is empty"),
        ],
    )
    def test_what_cannot_be_read_as_written_refuses_the_save(
        self, plugin, folder, said
    ):
        issues = _judge(plugin, [folder])
        assert [(i.path, i.index, i.severity) for i in issues] == [
            ("music_folders", 0, IssueSeverity.ERROR)
        ]
        assert said in issues[0].message

    def test_a_user_named_in_the_url_is_refused_with_the_url_to_write(self, plugin):
        issues = _judge(plugin, ["smb://alice@nas:4450/music"])

        assert [(i.index, i.severity) for i in issues] == [(0, IssueSeverity.ERROR)]
        assert "smb://nas:4450/music" in issues[0].message
        assert "alice" not in issues[0].message

    def test_a_user_already_in_a_url_does_not_refuse_an_unrelated_save(self, plugin):
        issues = _judge(
            plugin, ["smb://alice@nas/music"], changed=frozenset({"smb.password"})
        )

        assert all(i.severity == IssueSeverity.WARNING for i in issues)

    def test_the_same_folder_twice_is_refused_against_the_second_of_them(
        self, plugin
    ):
        _made, music = plugin
        issues = _judge(plugin, [str(music), str(music) + "/"])
        assert [(i.index, i.severity) for i in issues] == [(1, IssueSeverity.ERROR)]
        assert "entry 1" in issues[0].message

    def test_each_bad_entry_is_named_by_its_own_place_in_the_list(self, plugin):
        _made, music = plugin
        issues = _judge(plugin, [str(music), "smb://nas", "ftp://x/y"])
        assert [i.index for i in issues] == [1, 2]


class TestWhetherAFolderAnswers:
    def test_one_that_does_not_is_said_so_and_saved_anyway(self, plugin, tmp_path):
        issues = _judge(plugin, [str(tmp_path / "gone")])
        assert [(i.index, i.severity) for i in issues] == [(0, IssueSeverity.WARNING)]
        assert "until it can be read" in issues[0].message

    def test_a_folder_that_cannot_be_parsed_is_not_probed(self, plugin, monkeypatch):
        probed = []
        monkeypatch.setattr(
            LocalStorage,
            "probe_root_blocking",
            lambda self, root: probed.append(root) or RootStatus(root=root, available=True),
        )
        _judge(plugin, ["smb://nas"])
        assert probed == []


class TestWhenToBother:
    def test_a_change_touching_no_folder_is_not_probed(self, plugin, monkeypatch):
        probed = []
        monkeypatch.setattr(
            LocalStorage,
            "probe_root_blocking",
            lambda self, root: probed.append(root) or RootStatus(root=root, available=True),
        )
        assert _judge(plugin, changed=frozenset({"scan_interval_minutes"})) == []
        assert probed == []

    def test_a_credential_change_re_judges_the_folders_it_would_open(self, plugin):
        """The password is not part of any folder, but every share is read
        with it."""
        issues = _judge(
            plugin,
            ["smb://nas/music"],
            changed=frozenset({"smb.password"}),
        )
        assert [(i.index, i.severity) for i in issues] == [(0, IssueSeverity.WARNING)]

    def test_the_whole_credential_block_counts_as_a_credential_change(self, plugin):
        """A client may write ``smb`` outright rather than its leaves."""
        issues = _judge(
            plugin,
            ["smb://nas/music"],
            changed=frozenset({"smb"}),
        )
        assert [(i.index, i.severity) for i in issues] == [(0, IssueSeverity.WARNING)]

    def test_a_folder_that_was_already_wrong_does_not_refuse_a_password(
        self, plugin
    ):
        """Otherwise the entry has to be repaired before the credential that
        would make its neighbours readable can be saved at all."""
        _made, music = plugin
        issues = _judge(
            plugin,
            [str(music), "smb://nas"],
            changed=frozenset({"smb.password"}),
        )
        assert [i.severity for i in issues] == []


class TestWhatTheLiveConfigurationKeeps:
    def test_judging_does_not_move_playback_onto_unsaved_credentials(self, plugin):
        made, music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        before = made._resolver_for(live)

        _judge(plugin, [str(music)], changed=frozenset({"smb.password"}), smb={
            "username": "media", "password": "typed-but-not-saved", "encrypt": False
        })

        assert made._resolver_for(live) is before

    def test_judging_unchanged_credentials_reuses_the_resolver_playback_has(
        self, plugin
    ):
        """Its probe registry is what holds a hung share to one blocked
        worker thread, however often the page asks."""
        made, music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        assert made._resolver_for_candidate(live) is made._resolver_for(live)

    def test_judging_changed_credentials_uses_a_resolver_of_its_own(
        self, plugin
    ):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        staged = live.model_copy(deep=True)
        staged.smb.password = "typed-but-not-saved"
        assert made._resolver_for_candidate(staged) is not made._resolver_for(live)

    def test_judging_the_same_staged_credentials_twice_reuses_that_resolver(
        self, plugin
    ):
        """Every keystroke of a password is judged; a resolver per keystroke
        would strand a worker thread on each one when a share is hung."""
        made, _music = plugin
        staged = LocalFilesConfig(**made._context.config.model_dump())
        staged.smb.password = "typed-but-not-saved"
        assert made._resolver_for_candidate(staged) is made._resolver_for_candidate(
            staged
        )


class TestKeepingStagedCredentialsApart:
    """``smbclient``'s own pool is keyed on server and username and returns a
    matching session without re-checking the password. Two storages sharing
    it would answer for each other: a password typed into the settings page
    would be judged against the session playback is already using and called
    good whatever it was, and previewing encryption would turn it on for a
    session the protocol will not let it be turned off for again.
    """

    def test_two_storages_never_share_a_login(self):
        import smbclient._pool as pool

        live = SmbStorage(SmbCredentials(username="media", password="right"))
        staged = SmbStorage(SmbCredentials(username="media", password="wrong"))
        try:
            assert live._connections is not staged._connections
            assert live._connections is not pool._SMB_CONNECTIONS
            assert staged._connections is not pool._SMB_CONNECTIONS
        finally:
            live.close()
            staged.close()

    def test_every_call_is_made_against_this_storage_s_own_login(self):
        """One funnel, so no operation can fall back to the shared pool."""
        from kalinka_plugin_localfiles.storage.locator import parse

        storage = SmbStorage(SmbCredentials(username="media", password="p"))
        try:
            session = storage._session(parse("smb://nas/music"))
            assert session["connection_cache"] is storage._connections
        finally:
            storage.close()

    def test_a_storage_that_holds_nothing_releases_cleanly(self):
        SmbStorage().close()

    def test_a_storage_with_nothing_to_release_needs_no_close_of_its_own(self):
        """Local storage holds no connection, so the interface's own
        no-op is the whole of it."""
        LocalStorage().close()

    def test_the_resolver_releases_every_storage_it_holds(self):
        released = []

        class _Holding(FileStorage):
            def __init__(self, scheme):
                super().__init__()
                self._scheme = scheme

            @property
            def scheme(self):
                return self._scheme

            def handles(self, path):
                return False

            def canonical(self, path):
                return path

            def contains(self, path, roots):
                return False

            def listdir(self, path):
                return []

            def stat(self, path):
                raise OSError

            def open(self, path):
                raise OSError

            def local_path(self, path):
                return None

            def probe_root_blocking(self, root):
                return self.unavailable(root, "no")

            def close(self):
                released.append(self._scheme)

        resolver = StorageResolver([_Holding("a"), _Holding("b")])
        resolver.close()
        assert released == ["a", "b"]

    def test_one_storage_that_will_not_release_does_not_strand_the_others(self):
        released = []

        class _Storage(LocalStorage):
            def __init__(self, name, fails):
                super().__init__()
                self._name = name
                self._fails = fails

            @property
            def scheme(self):
                return self._name

            def close(self):
                if self._fails:
                    raise OSError("the server stopped answering")
                released.append(self._name)

        resolver = StorageResolver([_Storage("a", True), _Storage("b", False)])
        resolver.close()
        assert released == ["b"]


class TestLettingGoOfAReplacedResolver:
    """Each password typed is a credential set of its own, and each would
    otherwise leave a connection and the thread reading it behind."""

    def test_the_one_it_replaces_is_released(self, plugin):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        first = made._staged_resolvers.get(live)
        typed = live.model_copy(deep=True)
        typed.smb.password = "one-more-character"

        released = threading.Event()
        object.__setattr__(first, "close", released.set)
        made._staged_resolvers.get(typed)

        assert released.wait(timeout=5)

    def test_releasing_never_makes_the_page_wait(self, plugin):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        first = made._staged_resolvers.get(live)
        typed = live.model_copy(deep=True)
        typed.smb.password = "one-more-character"

        object.__setattr__(first, "close", lambda: time.sleep(1.0))
        started = time.monotonic()
        made._staged_resolvers.get(typed)

        assert time.monotonic() - started < 0.3

    def test_the_resolver_playback_reads_is_not_taken_away_mid_track(
        self, plugin
    ):
        """A saved credential change is followed by a restart within
        seconds; closing the connection a track is streaming from is a
        worse way to release it."""
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        reading = made._live_resolvers.get(live)
        saved = live.model_copy(deep=True)
        saved.smb.password = "just-saved"

        released = threading.Event()
        object.__setattr__(reading, "close", released.set)
        made._live_resolvers.get(saved)

        assert not released.wait(timeout=0.5)

    def test_closing_the_cache_releases_what_it_still_holds(self, plugin):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        held = made._staged_resolvers.get(live)

        released = threading.Event()
        object.__setattr__(held, "close", released.set)
        made._staged_resolvers.close()

        assert released.wait(timeout=5)
        assert made._staged_resolvers.get(live) is not held

    def test_shutting_down_releases_both_caches(self, plugin):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        released = []
        for cache in (made._live_resolvers, made._staged_resolvers):
            resolver = cache.get(live)
            object.__setattr__(
                resolver, "close", lambda name=id(cache): released.append(name)
            )

        asyncio.run(made.shutdown())

        for _ in range(50):
            if len(released) == 2:
                break
            time.sleep(0.1)
        assert len(released) == 2
