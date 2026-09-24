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

from kalinka_plugin_localfiles.config_model import (
    AccountSignIn,
    LocalFilesConfig,
    LocalLocation,
    LocalSource,
    SmbLocation,
    SmbOptions,
    SmbSource,
)
from kalinka_plugin_localfiles.storage import (
    SMB_SCHEME,
    StorageResolver,
    UnavailableStorage,
    build_resolver,
    library_roots,
    source_location,
)
from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.storage.smb import SmbStorage


def _share(id="nas", username=None, password="", encrypt=False, **location):
    fields = {"host": "nas", "path": "music", **location}
    return SmbSource(
        id=id,
        location=SmbLocation(**fields),
        authentication=(
            AccountSignIn(username=username, password=password)
            if username is not None
            else {"mode": "guest"}
        ),
        options=SmbOptions(require_encryption=encrypt),
    )


def _config(sources=()):
    return LocalFilesConfig(music_sources=list(sources))


def _resolver(sources=()):
    return build_resolver(_config(sources))


def _signed_in_as(resolver, path):
    return resolver.for_path(path)._credentials.username


class TestWhichStorageAnswers:
    def test_a_path_goes_to_the_local_filesystem(self):
        assert isinstance(_resolver().for_path("/srv/music/a.flac"), LocalStorage)

    def test_a_share_goes_to_a_client_signed_in_as_its_source(self):
        resolver = _resolver(sources=[_share(username="alice", password="p")])
        storage = resolver.for_path("smb://nas/music/a.flac")
        assert isinstance(storage, SmbStorage)
        assert storage._credentials.username == "alice"

    def test_two_sources_on_one_server_keep_their_own_logins(self):
        resolver = _resolver(
            sources=[
                _share(id="a", path="music", username="alice"),
                _share(id="b", path="films", username="bob"),
            ]
        )
        assert _signed_in_as(resolver, "smb://nas/music/a.flac") == "alice"
        assert _signed_in_as(resolver, "smb://nas/films/b.mkv") == "bob"

    def test_a_source_inside_another_is_read_with_its_own_login(self):
        resolver = _resolver(
            sources=[
                _share(id="outer", username="alice"),
                _share(id="inner", path="music/jazz", username="bob"),
            ]
        )
        assert _signed_in_as(resolver, "smb://nas/music/jazz/a.flac") == "bob"
        assert _signed_in_as(resolver, "smb://nas/music/rock/a.flac") == "alice"

    def test_sources_signing_in_alike_share_one_client(self):
        """And with it one connection per server, however many shares."""
        resolver = _resolver(
            sources=[
                _share(id="a", path="music", username="alice", password="p"),
                _share(id="b", path="films", username="alice", password="p"),
            ]
        )
        music, films = (resolver.for_path(f"smb://nas/{s}") for s in ("music", "films"))
        assert music is films

    def test_a_different_encryption_is_a_different_login(self):
        resolver = _resolver(
            sources=[
                _share(id="a", path="music", encrypt=True),
                _share(id="b", path="films"),
            ]
        )
        assert resolver.for_path("smb://nas/music") is not resolver.for_path(
            "smb://nas/films"
        )

    @pytest.mark.asyncio
    async def test_a_share_no_source_names_is_read_by_nobody(self):
        """Whatever the path, nothing may sign in to it on a guess — a row
        left from a removed source included."""
        resolver = _resolver(sources=[_share()])
        storage = resolver.for_path("smb://nas/other/a.flac")
        assert isinstance(storage, UnavailableStorage)
        status = await storage.probe_root("smb://nas/other")
        assert not status.available
        assert "music source" in status.reason

    def test_an_unknown_protocol_reports_rather_than_raises(self):
        storage = _resolver().for_path("ftp://host/music")
        assert isinstance(storage, UnavailableStorage)
        assert storage.scheme == "ftp"

    def test_an_unknown_protocol_answers_with_one_storage(self):
        """Callers group paths by the storage object to make one call per
        storage rather than per path, and the registry that keeps a hung root
        to a single probe lives on the instance. A fresh object per call
        quietly defeats both."""
        resolver = _resolver()
        first = resolver.for_path("ftp://host/music/a.flac")
        second = resolver.for_path("ftp://host/music/b.flac")

        assert first is second
        assert resolver.for_path("gopher://host/music") is not first

    @pytest.mark.asyncio
    async def test_an_unknown_protocol_names_itself_in_the_reason(self):
        storage = _resolver().for_path("ftp://host/music")
        status = await storage.probe_root("ftp://host/music")
        assert not status.available
        assert "ftp" in status.reason


class TestCanonicalRoots:
    def test_a_source_is_spelt_the_way_its_files_are_indexed(self):
        source = _share(host="NAS", port=445, path="music/")
        assert source_location(source) == "smb://nas/music"

    def test_a_folder_within_the_share_is_part_of_the_root(self):
        source = _share(port=4450, path="/music/Jazz//Modern/")
        assert source_location(source) == "smb://nas:4450/music/Jazz/Modern"

    def test_either_slash_separates_the_share_from_its_folders(self):
        source = _share(path="music\\Jazz \\ Modern")
        assert source_location(source) == "smb://nas/music/Jazz/Modern"

    def test_an_ipv6_server_is_bracketed(self):
        assert source_location(_share(host="fe80::1")) == "smb://[fe80::1]/music"

    def test_a_local_source_is_its_folder(self, tmp_path):
        source = LocalSource(id="usb", location=LocalLocation(path=str(tmp_path)))
        assert source_location(source) == str(tmp_path)

    def test_each_place_comes_once_in_the_order_of_the_sources(self, tmp_path):
        config = _config(
            sources=[
                _share(),
                LocalSource(id="usb", location=LocalLocation(path=str(tmp_path))),
                LocalSource(id="again", location=LocalLocation(path=str(tmp_path))),
            ],
        )
        assert library_roots(config, build_resolver(config)) == [
            "smb://nas/music",
            str(tmp_path),
        ]

    def test_a_share_url_among_the_folders_is_still_tidied(self):
        """It is not read, but it is compared against the sources."""
        assert _resolver().canonical_roots(["cifs://NAS:445/music/"]) == [
            "smb://nas/music"
        ]

    def test_blank_folders_are_dropped(self):
        assert _resolver().canonical_roots(["", "   "]) == []

    def test_a_misspelt_share_is_kept_as_written(self, caplog):
        """Dropping it would make it vanish from the settings page, and
        nothing indexed under it could be protected from the purge."""
        config = _config(sources=[_share(path="")])
        assert library_roots(config, build_resolver(config)) == ["smb://nas:445"]
        assert "share" in caplog.text

    def test_a_password_in_a_folder_url_is_not_logged(self, caplog):
        """The URL is refused, but a hand-edited config can still carry one
        to here, and the warning repeats on every status poll."""
        roots = _resolver().canonical_roots(["smb://alice:hunter2/x@nas/music"])
        assert "alice" in caplog.text
        assert "hunter2" not in caplog.text
        # The root is logged again by every scan that skips it.
        assert not any("hunter2" in root for root in roots)

    @pytest.mark.asyncio
    async def test_a_misspelt_share_says_what_is_wrong_with_it(self):
        config = _config(sources=[_share(path="")])
        resolver = build_resolver(config)
        [root] = library_roots(config, resolver)
        status = await resolver.for_path(root).probe_root(root)
        assert not status.available
        assert "smb://nas/music" in status.reason


class TestTheAccessBoundary:
    def test_a_share_path_is_compared_as_written(self):
        resolver = _resolver(sources=[_share()])
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

        resolver = _resolver(
            [LocalSource(id="music", location=LocalLocation(path=str(music)))]
        )
        roots = resolver.canonical_roots([str(music)])

        assert not resolver.within_roots(str(music / "link.flac"), roots)
        assert resolver.within_roots(str(music), roots)

    def test_roots_of_different_protocols_coexist(self, tmp_path):
        resolver = _resolver(sources=[_share()])
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
        resolver = _resolver(sources=[_share()])

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
        resolver = _resolver(sources=[_share()])

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

    def test_a_configured_root_wins_over_the_protocol(self):
        fallback = UnavailableStorage(SMB_SCHEME, "fallback")
        routed = SmbStorage()
        resolver = StorageResolver([fallback], {"smb://nas/music": routed})
        assert resolver.for_path("smb://nas/music/a.flac") is routed
        assert resolver.for_path("smb://nas/musical/a.flac") is fallback
