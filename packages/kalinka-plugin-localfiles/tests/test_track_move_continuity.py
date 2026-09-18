#!/usr/bin/env python3
"""
A renamed or moved file keeps its identity: same track id, same
first_indexed, evidence and enrichment intact — the paths are re-pointed via
(device, inode) detection instead of delete-plus-create. A copy (old file
still present) mints a new identity.
"""

import os

import aiosqlite
import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb


@pytest_asyncio.fixture
async def indexer(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    return FileIndexer(config, AsyncIndexerDb(config)), music, config


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(2205, dtype="float32"), 44100, format="FLAC")
    a = FLAC(str(path))
    for k, v in tags.items():
        a[k] = v
    a.save()


async def _row(config, sql, params=()):
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(sql, params)).fetchone()
        return dict(row) if row else None


async def _enrich(config, track_id):
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "UPDATE tracks SET mbid='rec-1', enriched=1 WHERE id=?", (track_id,)
        )
        await conn.execute(
            "UPDATE track_evidence SET fingerprint='FP' WHERE track_id=?",
            (track_id,),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_rename_keeps_identity(indexer):
    fi, music, config = indexer
    old = music / "Album" / "old name.flac"
    _flac(old, title="T", artist="A", album="X")
    track_id = (await fi.process_file(str(old)))["tracks"]
    await _enrich(config, track_id)
    lf = await _row(config, "SELECT * FROM library_file WHERE file_id=?", (track_id,))
    first_indexed = lf["first_indexed"]

    new = music / "Album" / "new name.flac"
    os.rename(old, new)
    result = await fi.process_file(str(new))
    assert result is None  # unchanged content: paths re-pointed, then skipped

    t = await _row(config, "SELECT * FROM tracks WHERE id=?", (track_id,))
    assert t is not None
    assert t["file_path"] == str(new)
    assert t["mbid"] == "rec-1"                 # enrichment intact
    lf = await _row(config, "SELECT * FROM library_file WHERE file_id=?", (track_id,))
    assert lf["current_path"] == str(new)
    assert lf["first_indexed"] == first_indexed  # not re-minted
    ev = await _row(config, "SELECT * FROM track_evidence WHERE track_id=?", (track_id,))
    assert ev["fingerprint"] == "FP"             # evidence intact
    n = await _row(config, "SELECT COUNT(*) c FROM tracks")
    assert n["c"] == 1                           # no duplicate row


@pytest.mark.asyncio
async def test_move_across_folders_keeps_identity(indexer):
    fi, music, config = indexer
    old = music / "Old Folder" / "01 song.flac"
    _flac(old, title="T", artist="A", album="X")
    track_id = (await fi.process_file(str(old)))["tracks"]

    new = music / "New Folder" / "01 song.flac"
    new.parent.mkdir(parents=True)
    os.rename(old, new)
    await fi.process_file(str(new))

    t = await _row(config, "SELECT * FROM tracks WHERE id=?", (track_id,))
    assert t["file_path"] == str(new)            # same id, new location


@pytest.mark.asyncio
async def test_copy_mints_new_identity(indexer):
    fi, music, config = indexer
    a = music / "Album" / "song.flac"
    _flac(a, title="T", artist="A", album="X")
    id_a = (await fi.process_file(str(a)))["tracks"]

    b = music / "Album" / "song copy.flac"
    import shutil

    shutil.copy2(a, b)  # old path still exists -> not a move
    id_b = (await fi.process_file(str(b)))["tracks"]

    assert id_b != id_a
    n = await _row(config, "SELECT COUNT(*) c FROM tracks")
    assert n["c"] == 2


@pytest.mark.asyncio
async def test_a_storage_without_file_identity_never_re_points_a_row(indexer):
    """Move detection needs an identity it can trust. A storage that reports
    none — a share whose server supplies no file index — must fall back to
    minting a new track rather than matching on whatever the lookup returns.

    Reported as a zero, the identity was shared by every file on the share:
    deleting one track and adding another of the same size handed the new
    file the old one's id, history and enrichment, and lost the old row.
    """
    from kalinka_plugin_localfiles.storage.base import FileStat
    from kalinka_plugin_localfiles.storage.local import LocalStorage

    fi, music, config = indexer
    first = music / "Album" / "first.flac"
    _flac(first, title="First", artist="A", album="X")

    real_stat = LocalStorage.stat

    def anonymous(self, path):
        measured = real_stat(self, path)
        return FileStat(
            size=measured.size,
            mtime_ns=measured.mtime_ns,
            is_dir=measured.is_dir,
            identity=None,
        )

    LocalStorage.stat = anonymous
    try:
        id_first = (await fi.process_file(str(first)))["tracks"]
        await _enrich(config, id_first)

        second = music / "Album" / "second.flac"
        _flac(second, title="Second", artist="A", album="X")
        os.remove(first)
        id_second = (await fi.process_file(str(second)))["tracks"]
    finally:
        LocalStorage.stat = real_stat

    assert id_second != id_first
    kept = await _row(config, "SELECT * FROM tracks WHERE id=?", (id_first,))
    assert kept["mbid"] == "rec-1"
    fresh = await _row(config, "SELECT * FROM tracks WHERE id=?", (id_second,))
    assert fresh["mbid"] is None
    assert fresh["file_path"] == str(second)


@pytest.mark.asyncio
async def test_a_file_dated_in_the_future_is_indexed_without_waiting(tmp_path):
    """The quiescence window compares a file's mtime against this clock. A
    NAS an hour ahead — or a Pi that has not reached an NTP server yet —
    produces a negative age, and subtracting it from the window made the
    indexer sleep out the whole skew, per file, forever.
    """
    import asyncio
    import time

    music = tmp_path / "music"
    music.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=5,
    )
    await init_db(config.db_path)
    fi = FileIndexer(config, AsyncIndexerDb(config))

    path = music / "Album" / "ahead.flac"
    _flac(path, title="T", artist="A", album="X")
    ahead = time.time() + 3600
    os.utime(path, (ahead, ahead))

    started = time.monotonic()
    result = await asyncio.wait_for(fi.process_file(str(path)), 10)
    assert result is not None
    assert time.monotonic() - started < 5
