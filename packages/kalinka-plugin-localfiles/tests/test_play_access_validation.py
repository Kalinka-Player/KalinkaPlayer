#!/usr/bin/env python3
"""Issue #60 (play side): access is validated whenever a track is played.

The play queue calls ``source_retriever()`` to get a track's source and treats
any exception as "track unavailable"; the server calls ``get_content_info()``
again before serving the bytes. A track whose file has moved out of the
configured music folders, or is no longer readable, must fail both — the
retriever by raising, the content lookup by reporting the asset absent.
"""

import pytest

from kalinka_plugin_sdk.inputmodule import ModuleAsset, SourceUnavailableError

import kalinka_plugin_localfiles.localfiles as localfiles_mod
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.storage import RootStatus
from kalinka_plugin_localfiles.storage.local import LocalStorage


class _FakeDb:
    def __init__(self, track, root_signature=None):
        self._track = track
        self._root_signature = root_signature

    def is_good(self):
        return True

    def get_root_signature(self, root):
        return self._root_signature

    def get_tracks_by_ids(self, track_ids):
        return [self._track] if self._track["id"] in track_ids else []

    def get_track_by_id(self, track_id):
        return self._track if self._track["id"] == track_id else None

    def get_album_by_id(self, album_id):
        # No artwork on disk in these tests; None keeps the cover lookup quiet.
        return None


def _module(tmp_path, music_folders, track, root_signature=None):
    config = LocalFilesConfig(
        music_folders=[str(f) for f in music_folders],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return LocalFilesInputModule(config, _FakeDb(track, root_signature))


def _track(file_path):
    return {
        "id": "track_1",
        "album_id": "album_1",
        "artist_id": "artist_1",
        "album_title": "Album",
        "artist_name": "Artist",
        "title": "Song",
        "duration": 100,
        "format": "audio/mpeg",
        "file_path": str(file_path),
    }


@pytest.mark.asyncio
async def test_source_retriever_names_an_asset_for_in_config_file(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"
    path.write_bytes(b"x")

    module = _module(tmp_path, [music], _track(path))
    [info] = await module.get_track_info(["track_1"])
    source = await info.source_retriever()
    assert source.source == ModuleAsset(module="localfiles", asset_id="track_1")
    assert source.format == "audio/mpeg"


@pytest.mark.asyncio
async def test_source_retriever_raises_for_out_of_config_file(tmp_path):
    music = tmp_path / "music"
    other = tmp_path / "other"
    music.mkdir()
    other.mkdir()
    path = other / "song.mp3"
    path.write_bytes(b"x")  # the file exists, but is out of scope

    module = _module(tmp_path, [music], _track(path))
    [info] = await module.get_track_info(["track_1"])
    with pytest.raises(PermissionError):
        await info.source_retriever()


@pytest.mark.asyncio
async def test_source_retriever_raises_for_missing_file(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"  # in config, but never created

    module = _module(tmp_path, [music], _track(path))
    [info] = await module.get_track_info(["track_1"])
    with pytest.raises(FileNotFoundError):
        await info.source_retriever()


def _offline(monkeypatch):
    """Report every root as an unmounted share, without the retry window."""

    async def unavailable(self, root, deadline_s=None):
        return RootStatus(
            root=root,
            available=False,
            empty=True,
            reason="the automounter has not mounted it",
            fs_type="autofs",
            is_network=False,
            is_autofs=True,
        )

    monkeypatch.setattr(LocalStorage, "await_root_available", unavailable)


@pytest.mark.asyncio
async def test_source_retriever_reports_unmounted_root_as_transient(
    tmp_path, monkeypatch
):
    music = tmp_path / "music"  # in config, never mounted: no dir at all
    path = music / "song.mp3"
    _offline(monkeypatch)

    module = _module(tmp_path, [music], _track(path))
    [info] = await module.get_track_info(["track_1"])
    with pytest.raises(SourceUnavailableError):
        await info.source_retriever()


@pytest.mark.asyncio
async def test_source_retriever_recovers_when_mount_appears(tmp_path, monkeypatch):
    music = tmp_path / "music"
    path = music / "song.mp3"

    async def mounts_late(self, root, deadline_s=None):
        # The share comes up during the wait — as a completing automount does.
        music.mkdir()
        path.write_bytes(b"x")
        return RootStatus(
            root=root,
            available=True,
            empty=False,
            reason="",
            fs_type="nfs4",
            is_network=True,
            is_autofs=True,
        )

    monkeypatch.setattr(LocalStorage, "await_root_available", mounts_late)

    module = _module(tmp_path, [music], _track(path))
    [info] = await module.get_track_info(["track_1"])
    source = await info.source_retriever()
    assert source.source == ModuleAsset(module="localfiles", asset_id="track_1")


@pytest.mark.asyncio
async def test_source_retriever_reports_identity_mismatch_as_transient(tmp_path):
    """The root stats fine but is not the filesystem the library was indexed
    from (a silently unmounted static share): transient, not file-gone."""
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"  # missing: it lives on the unmounted share

    module = _module(
        tmp_path, [music], _track(path), root_signature="nfs4 host:/export"
    )
    [info] = await module.get_track_info(["track_1"])
    with pytest.raises(SourceUnavailableError, match="nfs4 host:/export"):
        await info.source_retriever()


@pytest.mark.asyncio
async def test_a_stray_file_behind_a_lost_mount_is_not_served(tmp_path):
    """The mountpoint directory can hold files of its own once the share is
    gone. They stat perfectly, so readability alone would serve the wrong
    audio — the recorded mount identity has to be checked first."""
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"
    path.write_bytes(b"not the indexed file")

    module = _module(
        tmp_path, [music], _track(path), root_signature="nfs4 host:/export"
    )
    [info] = await module.get_track_info(["track_1"])
    with pytest.raises(SourceUnavailableError, match="nfs4 host:/export"):
        await info.source_retriever()


@pytest.mark.asyncio
async def test_source_retriever_bounds_a_hung_stat(tmp_path, monkeypatch):
    import time as time_mod

    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"
    path.write_bytes(b"x")

    module = _module(tmp_path, [music], _track(path))
    monkeypatch.setattr(localfiles_mod, "STAT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        module, "_require_readable", lambda _p: time_mod.sleep(0.5)
    )
    [info] = await module.get_track_info(["track_1"])
    with pytest.raises(SourceUnavailableError, match="did not respond"):
        await info.source_retriever()


@pytest.mark.asyncio
async def test_content_info_raises_for_unmounted_root(tmp_path, monkeypatch):
    music = tmp_path / "music"
    path = music / "song.mp3"
    _offline(monkeypatch)

    module = _module(tmp_path, [music], _track(path))
    # Transient unavailability must NOT read as "absent" (a 404 kills the
    # renderer's stream); the error carries through to the content route.
    with pytest.raises(SourceUnavailableError):
        await module.get_content_info("track_1")


@pytest.mark.asyncio
async def test_content_info_resolves_in_config_file(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"
    path.write_bytes(b"x")

    module = _module(tmp_path, [music], _track(path))
    info = await module.get_content_info("track_1")
    assert info is not None
    assert info.local_path == str(path)
    assert info.mime_type == "audio/mpeg"
    assert info.cacheable is True


@pytest.mark.asyncio
async def test_content_info_refuses_out_of_config_file(tmp_path):
    music = tmp_path / "music"
    other = tmp_path / "other"
    music.mkdir()
    other.mkdir()
    path = other / "song.mp3"
    path.write_bytes(b"x")

    module = _module(tmp_path, [music], _track(path))
    assert await module.get_content_info("track_1") is None


@pytest.mark.asyncio
async def test_content_info_refuses_missing_file(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"  # in config, but never created

    module = _module(tmp_path, [music], _track(path))
    assert await module.get_content_info("track_1") is None


@pytest.mark.asyncio
async def test_content_info_refuses_unknown_asset(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    path = music / "song.mp3"
    path.write_bytes(b"x")

    module = _module(tmp_path, [music], _track(path))
    assert await module.get_content_info("no_such_track") is None
