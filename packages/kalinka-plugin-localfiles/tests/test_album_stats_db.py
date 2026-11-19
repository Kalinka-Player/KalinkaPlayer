"""An album's track count and duration follow its tracks, for the indexer
and the enricher alike, and refreshing an album nothing happened to takes
no write lock: a rescan refreshes every album of the library."""

import aiosqlite
import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.enricher.enricher_db import AsyncEnricherDb
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb


@pytest_asyncio.fixture(params=[AsyncIndexerDb, AsyncEnricherDb])
async def db(request, tmp_path):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "art"),
    )
    await init_db(config.db_path)
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute("INSERT INTO artists (id, name) VALUES ('ar', 'Band')")
        await conn.execute(
            "INSERT INTO albums (id, title, artist_id) VALUES ('al', 'Album', 'ar')"
        )
        await conn.commit()
    return request.param(config)


async def _add_track(db, track_id, duration):
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute(
            "INSERT INTO tracks (id, title, file_path, format, album_id, duration) "
            "VALUES (?, ?, ?, 'flac', 'al', ?)",
            (track_id, track_id, f"/m/{track_id}.flac", duration),
        )
        await conn.commit()


async def _stats(db):
    async with aiosqlite.connect(db.db_path) as conn:
        cur = await conn.execute(
            "SELECT track_count, duration FROM albums WHERE id = 'al'"
        )
        return tuple(await cur.fetchone())


@pytest.mark.asyncio
async def test_stats_follow_the_albums_tracks(db):
    await _add_track(db, "t1", 100)
    await db.update_album_stats("al")
    assert await _stats(db) == (1, 100)

    await _add_track(db, "t2", 50)
    await db.update_album_stats("al")
    assert await _stats(db) == (2, 150)


@pytest.mark.asyncio
async def test_stats_already_right_are_left_alone(db, write_locked):
    await _add_track(db, "t1", 100)
    await db.update_album_stats("al")
    with write_locked(db.db_path):
        await db.update_album_stats("al")
    assert await _stats(db) == (1, 100)


@pytest.mark.asyncio
async def test_an_album_without_tracks_is_refreshed_once(db, write_locked):
    await db.update_album_stats("al")
    with write_locked(db.db_path):
        await db.update_album_stats("al")
    assert (await _stats(db))[0] == 0
