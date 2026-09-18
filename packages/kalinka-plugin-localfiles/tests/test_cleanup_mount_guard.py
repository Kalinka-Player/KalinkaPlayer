#!/usr/bin/env python3
"""The stale-track cleanup must never mistake an unmounted share for a
deleted library.

A root that is missing, unresponsive, or present-but-empty (the signature of
a mountpoint with nothing mounted on it) blocks purging of the rows under
it; and a root that goes offline between the sweep and the deletions is
caught by the pre-delete re-probe.
"""

import os

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.storage import RootStatus
from kalinka_plugin_localfiles.storage.local import LocalStorage


def _meta(**over):
    base = {"format": "audio/mpeg", "duration": 100}
    base.update(over)
    return base


async def _index_file(fi, path, **meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    fi._extract_metadata = lambda _s, _p, m=meta: _meta(**m)
    await fi.process_file(str(path))


def _make_indexer(tmp_path, folders):
    config = LocalFilesConfig(
        music_folders=[str(f) for f in folders],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    return FileIndexer(config, AsyncIndexerDb(config))


async def _async(value):
    return value


def _unavailable(root):
    return RootStatus(
        root=root,
        available=False,
        empty=True,
        reason="the automounter has not mounted it",
        fs_type="autofs",
        is_network=False,
        is_autofs=True,
    )


@pytest.fixture
def fast_probe(monkeypatch):
    """Skip the automount retry window: probe once, immediately."""

    async def probe_once(self, root, deadline_s=None):
        return await self.probe_root(root)

    monkeypatch.setattr(LocalStorage, "await_root_available", probe_once)


@pytest.mark.asyncio
async def test_tracks_under_empty_root_are_kept(tmp_path, fast_probe):
    music = tmp_path / "music"
    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")

    # The only file disappears and the root is left empty — exactly what an
    # unmounted share looks like. Nothing may be purged.
    os.remove(music / "a.mp3")
    removed = await fi.cleanup_stale_tracks()

    assert removed["tracks"] == 0
    assert len(await fi.db_manager.get_all_tracks()) == 1


@pytest.mark.asyncio
async def test_tracks_under_missing_root_are_kept(tmp_path, fast_probe):
    music = tmp_path / "music"
    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")

    os.remove(music / "a.mp3")
    os.rmdir(music)
    removed = await fi.cleanup_stale_tracks()

    assert removed["tracks"] == 0
    assert len(await fi.db_manager.get_all_tracks()) == 1


@pytest.mark.asyncio
async def test_missing_file_under_live_root_is_purged(tmp_path, fast_probe):
    music = tmp_path / "music"
    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")
    await _index_file(fi, music / "b.mp3", artist="B", album="BB", title="b")

    # One file gone, the root demonstrably alive (b.mp3 still there).
    os.remove(music / "a.mp3")
    removed = await fi.cleanup_stale_tracks()

    assert removed["tracks"] == 1
    paths = [t["file_path"] for t in await fi.db_manager.get_all_tracks()]
    assert paths == [str(music / "b.mp3")]


@pytest.mark.asyncio
async def test_root_going_offline_mid_sweep_blocks_the_deletes(
    tmp_path, fast_probe, monkeypatch
):
    music = tmp_path / "music"
    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")
    await _index_file(fi, music / "b.mp3", artist="B", album="BB", title="b")
    os.remove(music / "a.mp3")

    # First probe (the sweep's) sees the live root; the pre-delete re-probe
    # sees the share gone. The candidate must survive.
    real_probe = LocalStorage.probe_root
    calls = {"n": 0}

    async def probe(self, root, timeout=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            return _unavailable(root)
        return await real_probe(self, root)

    monkeypatch.setattr(LocalStorage, "probe_root", probe)
    removed = await fi.cleanup_stale_tracks()

    assert calls["n"] >= 2
    assert removed["tracks"] == 0
    assert len(await fi.db_manager.get_all_tracks()) == 2


@pytest.mark.asyncio
async def test_out_of_config_tracks_purge_even_with_root_offline(
    tmp_path, fast_probe, monkeypatch
):
    music = tmp_path / "music"
    dropped = tmp_path / "dropped"
    fi = _make_indexer(tmp_path, [music, dropped])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")
    await _index_file(fi, dropped / "b.mp3", artist="B", album="BB", title="b")

    # Restart with "dropped" out of config while "music" is also offline:
    # the config-scope purge must still run, the mount guard must still hold.
    monkeypatch.setattr(
        LocalStorage,
        "probe_root",
        lambda self, root, timeout=None: _async(_unavailable(root)),
    )
    os.remove(music / "a.mp3")
    os.rmdir(music)

    fi2 = _make_indexer(tmp_path, [music])
    removed = await fi2.cleanup_stale_tracks()

    assert removed["tracks"] == 1
    paths = [t["file_path"] for t in await fi2.db_manager.get_all_tracks()]
    assert paths == [str(music / "a.mp3")]


@pytest.mark.asyncio
async def test_run_scan_records_the_mount_identity(tmp_path, fast_probe):
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"x")

    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    fi._extract_metadata = lambda _s, _p: _meta(artist="A", album="AA", title="a")
    await fi.run_scan()

    signature = await fi.db_manager.get_root_signature(str(music))
    assert signature  # e.g. "tmpfs tmpfs" or "ext4 /dev/..."


@pytest.mark.asyncio
async def test_mount_identity_change_blocks_the_purge(tmp_path, fast_probe):
    """A static NFS mount silently unmounted: the mountpoint stats fine and
    may even hold a stray file, but the identity no longer matches what the
    library was indexed from — nothing may be purged."""
    music = tmp_path / "music"
    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")
    await fi.db_manager.set_root_signature(str(music), "nfs4 host:/export")

    os.remove(music / "a.mp3")
    (music / "sentinel.txt").write_bytes(b"stray")  # root non-empty, wrong fs
    removed = await fi.cleanup_stale_tracks()

    assert removed["tracks"] == 0
    assert len(await fi.db_manager.get_all_tracks()) == 1


@pytest.mark.asyncio
async def test_empty_root_with_matching_identity_purges(tmp_path, fast_probe):
    """Deleting the last file of a genuinely local folder must still clean
    the database: the recorded identity vouches that the storage itself did
    not go anywhere."""
    music = tmp_path / "music"
    fi = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, music / "a.mp3", artist="A", album="AA", title="a")
    current = await LocalStorage().probe_root(str(music))
    await fi.db_manager.set_root_signature(str(music), current.identity)

    os.remove(music / "a.mp3")  # root is now empty, identity unchanged
    removed = await fi.cleanup_stale_tracks()

    assert removed["tracks"] == 1
    assert await fi.db_manager.get_all_tracks() == []


@pytest.mark.asyncio
async def test_failure_cache_rows_under_offline_root_are_kept(
    tmp_path, fast_probe
):
    music = tmp_path / "music"
    music.mkdir()
    (music / "keepalive.mp3").write_bytes(b"x")
    offline = tmp_path / "offline"

    fi = _make_indexer(tmp_path, [music, offline])
    await init_db(fi.db_manager.db_path)
    await fi.db_manager.record_failure(str(music / "gone.mp3"), 1, 1, "boom")
    await fi.db_manager.record_failure(str(offline / "unreachable.mp3"), 1, 1, "boom")

    await fi.cleanup_stale_tracks()

    remaining = await fi.db_manager.get_failure_paths()
    assert remaining == [str(offline / "unreachable.mp3")]
