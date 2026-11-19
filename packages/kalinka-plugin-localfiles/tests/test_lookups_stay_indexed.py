"""Lookups made once per file or per album must not read a whole table.

A first index of 19,000 tracks spent half its database time in two that
did: the cost of each grows with the library, and of the index with its
square.
"""

import time

import aiosqlite
import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_connection import shared_connection
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb

TRACKS = 100_000
TRACKS_PER_ALBUM = 20
LOOKUPS = 300


@pytest_asyncio.fixture
async def db(tmp_path):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "art"),
    )
    await init_db(config.db_path)
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute("INSERT INTO artists (id, name) VALUES ('ar', 'Band')")
        await conn.executemany(
            "INSERT INTO albums (id, title, artist_id, track_count, duration) "
            "VALUES (?, ?, 'ar', ?, ?)",
            (
                (f"al{i}", f"Album {i}", TRACKS_PER_ALBUM, TRACKS_PER_ALBUM * 200)
                for i in range(TRACKS // TRACKS_PER_ALBUM)
            ),
        )
        await conn.executemany(
            "INSERT INTO tracks (id, title, file_path, format, album_id, artist_id, "
            "duration) VALUES (?, ?, ?, 'flac', ?, 'ar', 200)",
            (
                (f"t{i}", f"Song {i}", f"/m/{i}.flac", f"al{i // TRACKS_PER_ALBUM}")
                for i in range(TRACKS)
            ),
        )
        await conn.executemany(
            "INSERT INTO library_file (file_id, current_path, device_id, inode, "
            "first_indexed) VALUES (?, ?, 'dev', ?, 0)",
            ((f"t{i}", f"/m/{i}.flac", str(i)) for i in range(TRACKS)),
        )
        await conn.commit()
    return AsyncIndexerDb(config)


@pytest.mark.asyncio
async def test_finding_a_file_by_its_inode_stays_fast_in_a_large_library(db):
    start = time.monotonic()
    async with shared_connection(db.db_path):
        for i in range(LOOKUPS):
            found = await db.get_library_file_by_inode("dev", str(i * 300), "/elsewhere")
            assert found["file_id"] == f"t{i * 300}"
    elapsed = time.monotonic() - start
    # ~0.05s indexed; seconds when each lookup reads every file row.
    assert elapsed < 0.5, f"{LOOKUPS} lookups took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_refreshing_album_stats_stays_fast_in_a_large_library(db):
    start = time.monotonic()
    async with shared_connection(db.db_path):
        for i in range(LOOKUPS):
            await db.update_album_stats(f"al{i * 10}")
    elapsed = time.monotonic() - start
    # ~0.05s indexed; seconds when each refresh reads every track row.
    assert elapsed < 0.5, f"{LOOKUPS} refreshes took {elapsed:.2f}s"
