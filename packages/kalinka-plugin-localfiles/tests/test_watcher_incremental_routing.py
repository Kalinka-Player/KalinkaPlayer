#!/usr/bin/env python3
"""handle_incremental_changes routing for watcher events.

A directory moved into a watched folder arrives fully populated and emits
no per-file inotify events, so a ("dir_added", path) change must scan the
whole subtree — including nested directories. A ("path_removed", path)
change (file or directory deleted / moved out) has no per-path work but
must still run the stale-track cleanup that drops rows for missing files.
"""

import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb


@pytest_asyncio.fixture
async def indexer(tmp_path):
    """A FileIndexer backed by a real (temporary) sqlite database."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music_dir)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    db = AsyncIndexerDb(config)
    return FileIndexer(config, db), music_dir


def _instrument(fi, monkeypatch):
    """Record process_file/cleanup calls without touching the database."""
    processed, cleanups = [], []

    async def fake_process(path, listed=None):
        processed.append(path)
        return None

    async def fake_cleanup():
        cleanups.append(1)
        return {"tracks": 0, "albums": 0, "artists": 0}

    monkeypatch.setattr(fi, "process_file", fake_process)
    monkeypatch.setattr(fi, "cleanup_stale_tracks", fake_cleanup)
    return processed, cleanups


@pytest.mark.asyncio
async def test_dir_added_scans_the_whole_subtree(indexer, monkeypatch):
    fi, music_dir = indexer
    album = music_dir / "Album"
    disc2 = album / "Disc 2"
    disc2.mkdir(parents=True)
    (album / "01. one.flac").write_bytes(b"x")
    (disc2 / "02. two.mp3").write_bytes(b"x")
    (album / "cover.jpg").write_bytes(b"x")  # unsupported — must be skipped

    processed, cleanups = _instrument(fi, monkeypatch)

    await fi.handle_incremental_changes({("dir_added", str(album))})

    assert sorted(processed) == [
        str(album / "01. one.flac"),
        str(disc2 / "02. two.mp3"),
    ]
    assert cleanups  # cleanup always runs after a batch


@pytest.mark.asyncio
async def test_path_removed_runs_cleanup_only(indexer, monkeypatch):
    fi, music_dir = indexer
    processed, cleanups = _instrument(fi, monkeypatch)

    await fi.handle_incremental_changes(
        {
            ("path_removed", str(music_dir / "gone.flac")),
            ("path_removed", str(music_dir / "Gone Album")),
        }
    )

    assert processed == []  # nothing to index for removals
    assert cleanups  # but the stale-row cleanup must run
