#!/usr/bin/env python3
"""Sending a location to the storage that can read it.

The library holds paths, not protocols, so everything that reads a file asks
the resolver first. The contract that matters is that it always answers:
a folder naming a protocol nobody handles, or one that is misspelt, has to
come back as a root that reports why rather than as an exception thrown
through a scan — because a root that cannot be read is a state the library
already knows how to hold, and one that raises is not.
"""

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.storage import (
    SMB_SCHEME,
    StorageResolver,
    UnavailableStorage,
    build_resolver,
)
from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.storage.smb import SmbStorage


def _resolver(folders):
    return build_resolver(LocalFilesConfig(music_folders=folders))


class TestWhichStorageAnswers:
    def test_a_path_goes_to_the_local_filesystem(self):
        assert isinstance(_resolver([]).for_path("/srv/music/a.flac"), LocalStorage)

    def test_a_share_url_goes_to_the_smb_client(self):
        assert isinstance(_resolver([]).for_path("smb://nas/music"), SmbStorage)

    def test_cifs_reaches_the_same_client(self):
        assert isinstance(_resolver([]).for_path("cifs://nas/music"), SmbStorage)

    def test_an_unknown_protocol_reports_rather_than_raises(self):
        storage = _resolver([]).for_path("ftp://host/music")
        assert isinstance(storage, UnavailableStorage)
        assert storage.scheme == "ftp"

    @pytest.mark.asyncio
    async def test_an_unknown_protocol_names_itself_in_the_reason(self):
        storage = _resolver([]).for_path("ftp://host/music")
        status = await storage.probe_root("ftp://host/music")
        assert not status.available
        assert "ftp" in status.reason


class TestCanonicalRoots:
    def test_a_share_url_is_tidied(self):
        resolver = _resolver([])
        assert resolver.canonical_roots(["cifs://NAS:445/music/"]) == [
            "smb://nas/music"
        ]

    def test_blank_folders_are_dropped(self):
        assert _resolver([]).canonical_roots(["", "   "]) == []

    def test_a_misspelt_share_is_kept_as_written(self, caplog):
        """Dropping it would make it vanish from the settings page, and
        nothing indexed under it could be protected from the purge."""
        roots = _resolver([]).canonical_roots(["smb://nas"])
        assert roots == ["smb://nas"]
        assert "share" in caplog.text

    @pytest.mark.asyncio
    async def test_a_misspelt_share_says_what_is_wrong_with_it(self):
        resolver = _resolver([])
        status = await resolver.for_path("smb://nas").probe_root("smb://nas")
        assert not status.available
        assert "smb://nas/music" in status.reason


class TestTheAccessBoundary:
    def test_a_share_path_is_compared_as_written(self):
        resolver = _resolver([])
        roots = ["smb://nas/music"]
        assert resolver.within_roots("smb://nas/music/a/b.flac", roots)
        assert not resolver.within_roots("smb://nas/other/b.flac", roots)

    def test_a_local_symlink_out_of_the_folder_is_out_of_bounds(self, tmp_path):
        """The local storage resolves links, so a link under a music folder
        pointing elsewhere cannot be used to reach outside it."""
        music = tmp_path / "music"
        music.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.flac").write_bytes(b"x")
        (music / "link.flac").symlink_to(outside / "secret.flac")

        resolver = _resolver([str(music)])
        roots = resolver.canonical_roots([str(music)])

        assert not resolver.within_roots(str(music / "link.flac"), roots)
        assert resolver.within_roots(str(music), roots)

    def test_roots_of_different_protocols_coexist(self, tmp_path):
        resolver = _resolver([])
        roots = resolver.canonical_roots([str(tmp_path), "smb://nas/music"])
        assert resolver.root_of("smb://nas/music/a.flac", roots) == "smb://nas/music"
        assert resolver.root_of(str(tmp_path / "a.flac"), roots) == str(tmp_path)


class TestWhenTheSmbClientIsMissing:
    def test_share_folders_report_instead_of_breaking_the_plugin(self, monkeypatch):
        """smbprotocol is a hard dependency, but a broken install must cost
        the shares and not the whole module."""
        import sys

        monkeypatch.setitem(
            sys.modules, "kalinka_plugin_localfiles.storage.smb", None
        )
        resolver = build_resolver(LocalFilesConfig(music_folders=[]))

        storage = resolver.for_path("smb://nas/music")
        assert isinstance(storage, UnavailableStorage)
        assert storage.scheme == SMB_SCHEME
        assert isinstance(resolver.for_path("/srv/music"), LocalStorage)

    @pytest.mark.asyncio
    async def test_the_reason_says_the_library_is_not_installed(self, monkeypatch):
        import sys

        monkeypatch.setitem(
            sys.modules, "kalinka_plugin_localfiles.storage.smb", None
        )
        resolver = build_resolver(LocalFilesConfig(music_folders=[]))

        status = await resolver.for_path("smb://nas/music").probe_root(
            "smb://nas/music"
        )
        assert not status.available
        assert "not installed" in status.reason


class TestResolutionOrder:
    def test_the_first_storage_that_claims_the_protocol_wins(self):
        first = UnavailableStorage(SMB_SCHEME, "first")
        second = SmbStorage()
        resolver = StorageResolver([first, second])
        assert resolver.for_path("smb://nas/music") is first
