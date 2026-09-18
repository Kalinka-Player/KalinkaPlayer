#!/usr/bin/env python3
"""Tests for issue #60: a changed folder config must purge the database.

The local-files module may only access files under the parent folders named
in its config. When a folder is dropped from ``music_folders``, the rows for
its files must be removed on the next startup scan (they exist on disk but are
no longer in scope), and a play-time URL request for such a track must fail so
the play queue can flag it unavailable instead of handing the player a path the
module no longer manages.
"""

import os

import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.storage import build_resolver


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
    return FileIndexer(config, AsyncIndexerDb(config)), config


# --- access-boundary unit coverage ----------------------------------------


def test_within_roots_matches_by_component(tmp_path):
    root = tmp_path / "Music"
    sibling = tmp_path / "Music2"
    root.mkdir()
    sibling.mkdir()
    resolver = build_resolver(LocalFilesConfig(music_folders=[str(root)]))
    roots = resolver.canonical_roots([str(root)])

    assert resolver.within_roots(str(root / "a" / "song.mp3"), roots)
    assert resolver.within_roots(str(root), roots)  # the root itself
    # A sibling that merely shares a name prefix must NOT match.
    assert not resolver.within_roots(str(sibling / "song.mp3"), roots)
    assert not resolver.within_roots(str(tmp_path / "elsewhere.mp3"), roots)
    assert not resolver.within_roots("", roots)


# --- indexer cleanup on a changed folder config ------------------------------


@pytest.mark.asyncio
async def test_dropped_folder_tracks_are_purged_on_scan(tmp_path):
    keep = tmp_path / "keep"
    drop = tmp_path / "drop"

    # Index two files under two folders with the original config.
    fi, _ = _make_indexer(tmp_path, [keep, drop])
    await init_db(fi.db_manager.db_path)
    await _index_file(fi, keep / "a.mp3", artist="A", album="AA", title="a")
    await _index_file(fi, drop / "b.mp3", artist="B", album="BB", title="b")
    assert len(await fi.db_manager.get_all_tracks()) == 2

    # Restart with the "drop" folder removed from config. The file still
    # exists on disk, but is now out of scope.
    fi2, _ = _make_indexer(tmp_path, [keep])
    removed = await fi2.cleanup_stale_tracks()

    assert removed["tracks"] == 1
    paths = [t["file_path"] for t in await fi2.db_manager.get_all_tracks()]
    assert paths == [str((keep / "a.mp3"))]
    # The orphaned album/artist for the dropped track are gone too.
    assert removed["albums"] >= 1
    assert removed["artists"] >= 1


@pytest.mark.asyncio
async def test_failure_cache_rows_outside_config_are_dropped(tmp_path):
    keep = tmp_path / "keep"
    drop = tmp_path / "drop"
    keep.mkdir()
    drop.mkdir()
    inside = keep / "ok.mp3"
    outside = drop / "bad.mp3"
    inside.write_bytes(b"x")
    outside.write_bytes(b"x")

    fi, _ = _make_indexer(tmp_path, [keep])
    await init_db(fi.db_manager.db_path)
    await fi.db_manager.record_failure(str(inside), 1, 1, "boom")
    await fi.db_manager.record_failure(str(outside), 1, 1, "boom")

    await fi.cleanup_stale_tracks()

    remaining = await fi.db_manager.get_failure_paths()
    assert remaining == [str(inside)]


@pytest.mark.asyncio
async def test_symlink_escaping_roots_is_not_indexed(tmp_path):
    # A symlink physically under a configured folder but resolving outside it
    # must not be indexed — otherwise scan_folder would add it and
    # cleanup_stale_tracks (which resolves symlinks) would purge it on every
    # scan, churning the DB.
    music = tmp_path / "music"
    outside = tmp_path / "outside"
    music.mkdir()
    outside.mkdir()
    real = outside / "real.mp3"
    real.write_bytes(b"x")
    link = music / "link.mp3"
    link.symlink_to(real)

    fi, _ = _make_indexer(tmp_path, [music])
    await init_db(fi.db_manager.db_path)
    fi._extract_metadata = lambda _s, _p: _meta(artist="A", album="AA", title="a")
    result = await fi.process_file(str(link))

    assert result is None
    assert await fi.db_manager.get_all_tracks() == []


@pytest.mark.asyncio
async def test_missing_file_inside_config_still_purged(tmp_path):
    keep = tmp_path / "keep"
    fi, _ = _make_indexer(tmp_path, [keep])
    await init_db(fi.db_manager.db_path)
    path = keep / "gone.mp3"
    await _index_file(fi, path, artist="A", album="AA", title="a")
    # A sibling keeps the root alive: an empty unsigned root reads as an
    # unmounted share and blocks purging (see test_cleanup_mount_guard.py).
    await _index_file(fi, keep / "stays.mp3", artist="A", album="AA", title="s")
    os.remove(path)

    removed = await fi.cleanup_stale_tracks()
    assert removed["tracks"] == 1
    paths = [t["file_path"] for t in await fi.db_manager.get_all_tracks()]
    assert paths == [str(keep / "stays.mp3")]
