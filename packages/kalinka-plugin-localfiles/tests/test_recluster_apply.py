#!/usr/bin/env python3
"""
Tests for Phase-1 1g/2: the recluster applier. Indexes real files, runs
recluster(), and checks album grouping, album_cluster rows, aliasing and
idempotency.
"""

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
        folder_first_clustering=True,
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


async def _albums_of(config, track_ids):
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        out = {}
        for tid in track_ids:
            row = await (await conn.execute(
                "SELECT album_id FROM tracks WHERE id=?", (tid,))).fetchone()
            out[tid] = row["album_id"] if row else None
        return out


async def _scalar(config, sql, params=()):
    async with aiosqlite.connect(config.db_path) as conn:
        return (await (await conn.execute(sql, params)).fetchone())[0]


@pytest.mark.asyncio
async def test_tag_variants_merge_into_one_album(indexer):
    fi, music, config = indexer
    # Same folder, two album-tag variants that today would be two albums.
    folder = music / "Air - Everybody Hertz"
    ids = []
    for i in range(1, 7):
        p = folder / f"{i:02d}.flac"
        _flac(p, title=f"T{i}", artist="Air", albumartist="Air",
              album="Everybody Hertz", tracknumber=str(i))
        ids.append((await fi.process_file(str(p)))["tracks"])
    for i in range(7, 11):
        p = folder / f"{i:02d}.flac"
        _flac(p, title=f"T{i}", artist="Air", albumartist="Air",
              album="Air - Everybody Hertz", tracknumber=str(i))
        ids.append((await fi.process_file(str(p)))["tracks"])

    # Before reclustering: two distinct albums (the fragmentation bug).
    assert len(set((await _albums_of(config, ids)).values())) == 2

    await fi.recluster()

    albums = await _albums_of(config, ids)
    assert len(set(albums.values())) == 1  # one album now
    # album_cluster row exists for it.
    assert await _scalar(config, "SELECT COUNT(*) FROM album_cluster") >= 1


@pytest.mark.asyncio
async def test_untagged_folder_gets_named_album(indexer):
    fi, music, config = indexer
    folder = music / "Some Bootleg"
    ids = []
    for i in range(1, 5):
        p = folder / f"{i:02d}.flac"
        _flac(p, artist="Unknown", tracknumber=str(i))  # no album tag
        ids.append((await fi.process_file(str(p)))["tracks"])

    await fi.recluster()

    albums = await _albums_of(config, ids)
    assert len(set(albums.values())) == 1
    album_id = next(iter(albums.values()))
    assert album_id != "unknown_album"
    title = await _scalar(config, "SELECT title FROM albums WHERE id=?", (album_id,))
    assert title == "Some Bootleg"


@pytest.mark.asyncio
async def test_recluster_cleans_path_artifact_title(indexer):
    fi, music, config = indexer
    folder = music / "1993. Jean Michel Jarre - Chronology (Sony 88875129492, Russia)"
    for i in range(1, 5):
        _flac(folder / f"{i}. Track {i}.flac", title=f"T{i}",
              artist="Jean Michel Jarre", albumartist="Jean Michel Jarre",
              album="Chronology (Sony 88875129492, Russia)", tracknumber=str(i))
        await fi.process_file(str(folder / f"{i}. Track {i}.flac"))

    await fi.recluster()

    title = await _scalar(
        config, "SELECT title FROM albums WHERE id IN "
        "(SELECT DISTINCT album_id FROM tracks WHERE file_path LIKE ?)",
        (f"{folder}%",))
    assert title == "Chronology"   # catalog parenthetical stripped


@pytest.mark.asyncio
async def test_recluster_idempotent(indexer):
    fi, music, config = indexer
    folder = music / "Album X"
    for i in range(1, 6):
        _flac(folder / f"{i:02d}.flac", title=f"T{i}", artist="Band",
              albumartist="Band", album="Album X", tracknumber=str(i))
        await fi.process_file(str(folder / f"{i:02d}.flac"))

    r1 = await fi.recluster()
    snapshot = await _scalar(config, "SELECT COUNT(*) FROM tracks")
    r2 = await fi.recluster()
    # Second run re-points nothing and creates no aliases (semantic no-op).
    assert r2["reassigned"] == 0
    assert r2["aliases"] == 0
    assert await _scalar(config, "SELECT COUNT(*) FROM tracks") == snapshot


@pytest.mark.asyncio
async def test_recluster_of_an_unchanged_library_writes_nothing(indexer, write_locked):
    fi, music, config = indexer
    for album in ("Album X", "Album Y"):
        folder = music / "Band" / album
        for i in range(1, 4):
            _flac(folder / f"{i:02d}.flac", title=f"T{i}", artist="Band",
                  albumartist="Band", album=album, tracknumber=str(i))
            await fi.process_file(str(folder / f"{i:02d}.flac"))
    await fi.recluster()
    generations = "SELECT COALESCE(SUM(generation), 0) FROM album_cluster"
    before = await _scalar(config, generations)

    with write_locked(config.db_path):
        again = await fi.recluster()

    assert again["reassigned"] == 0
    assert await _scalar(config, generations) == before
