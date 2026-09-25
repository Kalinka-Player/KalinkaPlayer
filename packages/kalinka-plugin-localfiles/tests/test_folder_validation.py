"""Telling the user a music folder or source is wrong, while they can still fix it.

Two kinds of wrong, answered differently. How a location is written is the
user's mistake and refuses the save, because nothing about it will improve
by keeping it. Whether it answers right now is not: a NAS switched off
tonight is still the right source to have configured, and the library holds
what it indexed under a root it cannot currently see.

The music folders are the local sources again, as an older app edits them.
What that app wrote is judged against its folders, and the shares it cannot
see are left out of it; a newer app writes the sources and hears about each
by its id.

The logins are the trap here. A password the user has typed but not saved
must reach the probe that judges the share, and must not reach the resolver
playback reads.
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


def _share(
    id="nas", host="nas", path="music", username=None, password="", encrypt=False
):
    authentication = (
        {"mode": "account", "username": username, "password": password}
        if username is not None
        else {"mode": "guest"}
    )
    return {
        "id": id,
        "kind": "smb",
        "location": {"host": host, "port": 445, "path": path},
        "authentication": authentication,
        "options": {"require_encryption": encrypt},
    }


def _local(path, id="usb"):
    return {"id": id, "kind": "local", "location": {"path": str(path)}}


@pytest.fixture
def plugin(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    made = KalinkaPluginLocalFiles()
    made._context = _Context(
        LocalFilesConfig(
            music_folders=[str(music)],
            music_sources=[_share(username="media", password="saved")],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
    )
    return made, music


def _answered(root: str) -> RootStatus:
    return RootStatus(
        root=root,
        available=True,
        reason="",
        fs_type=None,
        is_network=False,
        is_autofs=False,
    )


@pytest.fixture
def shares(monkeypatch):
    """Every share answers, and says with whose login it was asked."""
    asked = []

    def probe(self, root):
        asked.append((root, self._credentials))
        return _answered(root)

    monkeypatch.setattr(SmbStorage, "probe_root_blocking", probe)
    return asked


@pytest.fixture
def folders(monkeypatch):
    """Every local folder answers, and says it was asked."""
    asked = []

    def probe(self, root):
        asked.append(root)
        return _answered(root)

    monkeypatch.setattr(LocalStorage, "probe_root_blocking", probe)
    return asked


def _judge(plugin, folders=None, sources=None, changed=None):
    """What the dry run says: the change written onto a copy of the live
    configuration and reconciled, as the server does."""
    made, _music = plugin
    candidate = made._context.config.model_copy(deep=True)
    written = set()
    if folders is not None:
        candidate.music_folders = folders
        written.add("music_folders")
    if sources is not None:
        candidate.music_sources = LocalFilesConfig(music_sources=sources).music_sources
        written.add("music_sources")
    candidate.reconcile(frozenset(written))
    return asyncio.run(
        made.validate_config(candidate, frozenset(changed or written))
    )


def _saved(plugin, *sources):
    """The live configuration as a save of ``sources`` leaves it."""
    made, _music = plugin
    live = made._context.config
    live.music_sources = LocalFilesConfig(music_sources=list(sources)).music_sources
    live.reconcile(frozenset({"music_sources"}))


def _with_password(config, password):
    typed = config.model_copy(deep=True)
    typed.music_sources[0].authentication.password = password
    return typed


class TestHowAFolderIsWritten:
    def test_a_folder_that_is_there_draws_nothing(self, plugin):
        _made, music = plugin
        assert _judge(plugin, [str(music)]) == []

    @pytest.mark.parametrize(
        "folder, said",
        [
            ("smb://nas/music", "music source"),
            ("cifs://nas/music", "music source"),
            (r"\\nas\music", "music source"),
            ("ftp://nas/music", "music source"),
            ("smb:/nas/music", "needs two slashes"),
            ("", "name the folder"),
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

    def test_a_share_is_refused_without_being_asked_about(self, plugin, shares):
        _judge(plugin, ["smb://nas/music"])
        assert shares == []

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


class TestHowASourceIsWritten:
    def test_a_share_that_answers_draws_nothing(self, plugin, shares):
        assert _judge(plugin, sources=[_share()]) == []

    def test_a_local_source_that_is_there_draws_nothing(self, plugin, tmp_path):
        disk = tmp_path / "disk"
        disk.mkdir()
        assert _judge(plugin, sources=[_local(disk)]) == []

    @pytest.mark.parametrize(
        "source, part, said",
        [
            (_share(host=""), "location.host", "name the server"),
            (_share(host="alice@nas"), "location.host", "nothing more"),
            (_share(host="nas:4450"), "location.host", "nothing more"),
            (_share(path=""), "location.path", "name the share"),
            (_share(path="/ / "), "location.path", "name the share"),
            (_share(path="music/a/../b"), "location.path", "'..'"),
            (_share(username=""), "authentication.username", "name the account"),
            (_share(encrypt=True), "options.require_encryption", "a guest cannot"),
            (_local(""), "location.path", "name the folder"),
            (_local("smb://nas/music"), "location.path", "on the server"),
            (_local(r"\\nas\music"), "location.path", "on the server"),
        ],
    )
    def test_a_part_that_cannot_be_right_is_refused_by_name(
        self, plugin, shares, source, part, said
    ):
        issues = _judge(plugin, sources=[source])
        assert [(i.path, i.index, i.severity) for i in issues] == [
            (f"music_sources.{source['id']}.{part}", None, IssueSeverity.ERROR)
        ]
        assert said in issues[0].message

    def test_an_issue_follows_its_source_wherever_it_moves(self, plugin, shares):
        """Addressed by id, so reordering the list cannot move an error onto
        a neighbour."""
        broken = _share(id="broken", path="")
        for sources in ([_share(), broken], [broken, _share()]):
            issues = _judge(plugin, sources=sources)
            assert [i.path for i in issues] == ["music_sources.broken.location.path"]

    def test_the_same_share_twice_is_refused_against_the_second(self, plugin, shares):
        issues = _judge(
            plugin,
            sources=[_share(id="a"), _share(id="b", host="NAS", path="//music/")],
        )
        assert [(i.path, i.severity) for i in issues] == [
            ("music_sources.b", IssueSeverity.ERROR)
        ]
        assert issues[0].message == "the same place as another source"

    def test_a_misspelt_source_does_not_refuse_an_edit_of_the_folders(self, plugin):
        """The app editing the folders may not know the sources exist."""
        made, music = plugin
        made._context.config.music_sources = LocalFilesConfig(
            music_sources=[_share(path="")]
        ).music_sources
        assert _judge(plugin, [str(music)]) == []

    def test_an_entry_already_wrong_does_not_refuse_the_rest_of_the_list(
        self, plugin, shares
    ):
        """Otherwise it has to be repaired before a password for the share
        beside it can be saved at all."""
        left_over = _local("smb://nas/music", id="old")
        _saved(plugin, left_over, _share(username="media", password="saved"))
        issues = _judge(
            plugin, sources=[left_over, _share(username="media", password="typed")]
        )
        assert [(i.path, i.severity) for i in issues] == [
            ("music_sources.old.location.path", IssueSeverity.WARNING)
        ]

    def test_an_entry_already_wrong_is_refused_once_it_is_edited(self, plugin):
        _saved(plugin, _local("smb://nas/music", id="old"))
        issues = _judge(plugin, sources=[_local("smb://nas/films", id="old")])
        assert [i.path for i in issues] == ["music_sources.old.location.path"]


class TestAnOlderAppsFolders:
    def test_a_folder_is_judged_as_the_local_source_it_is(self, plugin):
        """Said against the folder, in the words of the list that app shows."""
        issues = _judge(plugin, ["", "smb://nas/music"])
        assert [(i.path, i.index) for i in issues] == [
            ("music_folders", 0),
            ("music_folders", 1),
        ]
        assert "up-to-date Kalinka app" in issues[1].message

    def test_a_folder_already_a_local_source_is_told_by_its_place(
        self, plugin, tmp_path
    ):
        _made, music = plugin
        issues = _judge(plugin, [str(music), str(music)])
        assert [(i.path, i.index, i.message) for i in issues] == [
            ("music_folders", 1, "the same folder as entry 1")
        ]


class TestWhetherItAnswers:
    def test_a_folder_that_does_not_is_said_so_and_saved_anyway(self, plugin, tmp_path):
        issues = _judge(plugin, [str(tmp_path / "gone")])
        assert [(i.index, i.severity) for i in issues] == [(0, IssueSeverity.WARNING)]
        assert "until it can be read" in issues[0].message

    def test_a_share_that_does_not_is_said_so_against_its_source(
        self, plugin, monkeypatch
    ):
        monkeypatch.setattr(
            SmbStorage,
            "probe_root_blocking",
            lambda self, root: self.unavailable(root, "nas did not answer"),
        )
        issues = _judge(plugin, sources=[_share()])
        assert [(i.path, i.severity) for i in issues] == [
            ("music_sources.nas", IssueSeverity.WARNING)
        ]
        assert "nas did not answer" in issues[0].message

    def test_a_folder_that_cannot_be_parsed_is_not_probed(self, plugin, folders):
        _judge(plugin, ["smb://nas"])
        assert folders == []

    def test_a_share_is_asked_with_the_login_its_source_names(self, plugin, shares):
        _judge(
            plugin,
            sources=[
                _share(id="a", path="music", username="alice", password="one"),
                _share(id="b", path="films"),
            ],
        )
        assert sorted((root, c.username, c.password) for root, c in shares) == [
            ("smb://nas/films", "", ""),
            ("smb://nas/music", "alice", "one"),
        ]


class TestWhenToBother:
    def test_a_change_touching_neither_list_is_not_probed(
        self, plugin, folders, shares
    ):
        assert _judge(plugin, changed={"scan_interval_minutes"}) == []
        assert folders == [] and shares == []

    def test_editing_the_folders_asks_nothing_of_the_shares(self, plugin, shares):
        _made, music = plugin
        _judge(plugin, [str(music)])
        assert shares == []

    def test_editing_the_sources_asks_nothing_of_the_folders(
        self, plugin, folders, shares
    ):
        _judge(plugin, sources=[_share()])
        assert folders == []

    def test_a_new_password_re_asks_its_share(self, plugin, shares):
        """The login is part of the source, so typing it edits the list."""
        _judge(plugin, sources=[_share(username="media", password="typed")])
        assert [c.password for _root, c in shares] == ["typed"]


class TestWhatTheLiveConfigurationKeeps:
    def test_judging_does_not_move_playback_onto_unsaved_credentials(
        self, plugin, shares
    ):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        before = made._resolver_for(live)

        _judge(
            plugin, sources=[_share(username="media", password="typed-but-not-saved")]
        )

        assert made._resolver_for(live) is before

    def test_a_folder_is_asked_through_the_storage_playback_reads(
        self, plugin, shares, monkeypatch
    ):
        """Its registry holds a hung folder to one blocked thread. A storage
        of its own for every keystroke in a share's server would claim a
        thread for each, from the executor every request shares."""
        made, music = plugin
        reading = made._current_resolver().for_path(str(music))
        asked = []
        monkeypatch.setattr(
            LocalStorage,
            "probe_root_blocking",
            lambda self, root: asked.append(self) or _answered(root),
        )

        for host in ("n", "na", "nas2"):
            _judge(plugin, sources=[_local(music, id="music"), _share(host=host)])

        assert len(asked) == 3
        assert all(storage is reading for storage in asked)

    def test_judging_unchanged_logins_reuses_the_resolver_playback_has(self, plugin):
        """Its probe registry is what holds a hung share to one blocked
        worker thread, however often the page asks."""
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        assert made._resolver_for_candidate(live) is made._resolver_for(live)

    def test_judging_a_changed_login_uses_a_resolver_of_its_own(self, plugin):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        staged = _with_password(live, "typed-but-not-saved")
        assert made._resolver_for_candidate(staged) is not made._resolver_for(live)

    def test_judging_a_new_share_uses_a_resolver_of_its_own(self, plugin):
        """The live one has no login for a share it was not built with."""
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        staged = LocalFilesConfig(
            **{**live.model_dump(), "music_sources": [_share(path="films")]}
        )
        assert made._resolver_for_candidate(staged) is not made._resolver_for(live)

    def test_judging_the_same_staged_login_twice_reuses_that_resolver(self, plugin):
        """Every keystroke of a password is judged; a resolver per keystroke
        would strand a worker thread on each one when a share is hung."""
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        staged = _with_password(live, "typed-but-not-saved")
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

    def test_a_password_is_never_part_of_how_a_login_prints(self):
        assert "hunter2" not in repr(SmbCredentials("media", "hunter2"))

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

        routed = _Holding("c")
        resolver = StorageResolver(
            [_Holding("a"), _Holding("b")], {"smb://x/1": routed, "smb://x/2": routed}
        )
        resolver.close()
        assert released == ["a", "b", "c"]

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
        typed = _with_password(live, "one-more-character")

        released = threading.Event()
        object.__setattr__(first, "close", released.set)
        made._staged_resolvers.get(typed)

        assert released.wait(timeout=5)

    def test_releasing_never_makes_the_page_wait(self, plugin):
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        first = made._staged_resolvers.get(live)
        typed = _with_password(live, "one-more-character")

        object.__setattr__(first, "close", lambda: time.sleep(1.0))
        started = time.monotonic()
        made._staged_resolvers.get(typed)

        assert time.monotonic() - started < 0.3

    def test_the_resolver_playback_reads_is_not_taken_away_mid_track(
        self, plugin
    ):
        """A saved login change is followed by a restart within seconds;
        closing the connection a track is streaming from is a worse way to
        release it."""
        made, _music = plugin
        live = LocalFilesConfig(**made._context.config.model_dump())
        reading = made._live_resolvers.get(live)
        saved = _with_password(live, "just-saved")

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
