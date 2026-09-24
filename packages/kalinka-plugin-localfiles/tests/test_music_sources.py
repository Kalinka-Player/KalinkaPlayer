"""Music sources: where the library reads from, one record each.

A folder list says where; a source also says how to get there. Local and SMB
are the two kinds, told apart by ``kind``, and an SMB source signs in as a
guest or with an account of its own, told apart by ``mode``.

The music folders an older app edits are the local sources again. Whichever
of the two lists is written, the other follows it, so the two apps never
disagree about where the music is.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kalinka_plugin_localfiles.config_model import (
    AccountSignIn,
    GuestSignIn,
    LocalFilesConfig,
    LocalSource,
    SmbSource,
)


def _sources(*raw):
    return LocalFilesConfig(music_sources=list(raw)).music_sources


def _share(**fields):
    return {
        "id": "nas",
        "kind": "smb",
        "location": {"host": "nas", "path": "music"},
        **fields,
    }


class TestTheShape:
    def test_a_library_starts_with_the_starter_folder_in_both_lists(self):
        config = LocalFilesConfig()
        [starter] = config.music_sources
        assert isinstance(starter, LocalSource)
        assert config.music_folders == [starter.location.path]

    def test_kind_decides_what_a_source_is(self):
        local, share = _sources(
            {"kind": "local", "location": {"path": "/mnt/usb"}}, _share()
        )
        assert isinstance(local, LocalSource)
        assert isinstance(share, SmbSource)

    def test_a_share_signs_in_as_a_guest_unless_told_otherwise(self):
        [share] = _sources(_share())
        assert isinstance(share.authentication, GuestSignIn)
        assert share.location.port == 445
        assert share.options.require_encryption is False

    def test_mode_decides_how_a_share_signs_in(self):
        [share] = _sources(
            _share(authentication={"mode": "account", "username": "media"})
        )
        assert isinstance(share.authentication, AccountSignIn)

    def test_a_kind_nobody_reads_is_refused(self):
        with pytest.raises(ValidationError):
            _sources({"kind": "webdav", "location": {}})

    def test_two_sources_with_one_id_are_refused(self):
        with pytest.raises(ValidationError, match="nas"):
            _sources(_share(), _share())


class TestWhatASavedPasswordIsFor:
    """One account on one server: pointing a source elsewhere, or at another
    account, must not carry the saved password with it."""

    _ACCOUNT = {"mode": "account", "username": "media"}

    def _scope(self, **fields):
        [share] = _sources({**_share(authentication=self._ACCOUNT), **fields})
        return share.credential_scope()

    def test_moving_within_the_server_keeps_it(self):
        assert self._scope() == self._scope(
            location={"host": "NAS", "path": "films/new"}
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"location": {"host": "other", "path": "music"}},
            {"location": {"host": "nas", "port": 4450, "path": "music"}},
            {"authentication": {"mode": "account", "username": "someone-else"}},
        ],
    )
    def test_another_server_or_account_does_not(self, fields):
        assert self._scope(**fields) != self._scope()

    def test_it_is_never_part_of_how_a_source_prints(self):
        [share] = _sources(
            _share(authentication={**self._ACCOUNT, "password": "hunter2"})
        )
        assert "hunter2" not in repr(share)


def _local(path, id=None):
    return {"kind": "local", "location": {"path": path}, **({"id": id} if id else {})}


def _written(config, **fields):
    """As the server writes a change: set, then reconcile."""
    for name, value in fields.items():
        setattr(config, name, value)
    config.reconcile(frozenset(fields))
    return config


def _paths(config):
    return [
        source.location.path if isinstance(source, LocalSource) else source.id
        for source in config.music_sources
    ]


class TestTheFoldersAnOlderAppSees:
    def test_they_are_the_local_sources_and_no_share(self):
        config = LocalFilesConfig(
            music_sources=[_local("/mnt/usb"), _share(), _local("/srv/music")]
        )
        assert config.music_folders == ["/mnt/usb", "/srv/music"]

    def test_they_follow_a_write_of_the_sources(self):
        config = _written(LocalFilesConfig(), music_sources=_sources(_local("/mnt/usb")))
        assert config.music_folders == ["/mnt/usb"]

    def test_saved_folders_become_local_sources(self):
        """What a 5.0 library kept, read by this version for the first time."""
        config = LocalFilesConfig(music_folders=["/mnt/usb", "/srv/music"])
        assert _paths(config) == ["/mnt/usb", "/srv/music"]
        assert all(isinstance(s, LocalSource) for s in config.music_sources)


class TestWritingTheFolders:
    """An older app writes the folders; the local sources follow them."""

    def _library(self):
        return LocalFilesConfig(
            music_sources=[_local("/mnt/usb", id="usb"), _share(), _local("/srv", id="srv")]
        )

    def test_a_folder_kept_keeps_its_source(self):
        config = _written(self._library(), music_folders=["/mnt/usb", "/srv"])
        assert [s.id for s in config.music_sources] == ["usb", "nas", "srv"]

    def test_a_folder_removed_takes_its_source_along(self):
        """The local sources fill the places local sources held, in the
        folders' order, so the folders read back as they were written."""
        config = _written(self._library(), music_folders=["/srv"])
        assert [s.id for s in config.music_sources] == ["srv", "nas"]

    def test_a_folder_added_is_a_new_source(self):
        config = _written(self._library(), music_folders=["/mnt/usb", "/srv", "/new"])
        assert _paths(config) == ["/mnt/usb", "nas", "/srv", "/new"]
        assert config.music_sources[3].id not in {"usb", "nas", "srv"}

    def test_the_folders_order_is_the_local_sources_order(self):
        config = _written(self._library(), music_folders=["/srv", "/mnt/usb"])
        assert [s.id for s in config.music_sources] == ["srv", "nas", "usb"]
        assert config.music_folders == ["/srv", "/mnt/usb"]

    def test_a_share_is_never_touched(self):
        config = _written(self._library(), music_folders=[])
        [share] = config.music_sources
        assert share.id == "nas"

    def test_one_folder_twice_is_two_sources_to_be_told_off(self):
        """So the second can be named by its place in the list."""
        config = _written(LocalFilesConfig(), music_folders=["/a", "/a"])
        assert _paths(config) == ["/a", "/a"]
        assert config.music_sources[0].id != config.music_sources[1].id
