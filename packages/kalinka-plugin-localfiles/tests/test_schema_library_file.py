#!/usr/bin/env python3
"""
Tests for the Phase-0 evidence schema (library_file + track_evidence):

  1. A fresh DB has both tables with the expected columns.
  2. Re-running init_db over a legacy DB backfills library_file from the
     existing tracks (path-derived id becomes the initial file_id).
  3. The backfill is idempotent — a second init_db adds no rows and
     leaves an existing library_file row untouched.
"""

import aiosqlite
import pytest

from kalinka_plugin_localfiles.db_schema import init_db

LIBRARY_FILE_COLS = {
    "file_id",
    "current_path",
    "size_bytes",
    "modified_at",
    "device_id",
    "inode",
    "content_hash",
    "first_indexed",
}
TRACK_EVIDENCE_COLS = {
    "track_id",
    "raw_tags",
    "stream_info",
    "art_phash",
    "art_type",
    "cue_sheet",
    "cue_tracks",
    "fingerprint",
    "fp_computed_at",
    "import_batch",
    "updated_at",
}


async def _cols(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_fresh_db_has_evidence_tables(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as conn:
        assert await _cols(conn, "library_file") == LIBRARY_FILE_COLS
        assert await _cols(conn, "track_evidence") == TRACK_EVIDENCE_COLS
        cur = await conn.execute("SELECT COUNT(*) FROM library_file")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_backfill_from_existing_tracks(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    # A legacy DB predating library_file: just the tracks columns the
    # backfill reads.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                file_path TEXT NOT NULL, format TEXT NOT NULL,
                file_size BIGINT, modified_time INTEGER, last_updated INTEGER
            )
            """
        )
        await conn.executemany(
            "INSERT INTO tracks (id, title, file_path, format, file_size, "
            "modified_time, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("track_aaa", "A", "/m/a.flac", "audio/flac", 111, 1000, 1710000000),
                ("track_bbb", "B", "/m/b.flac", "audio/flac", 222, 2000, 1710000001),
            ],
        )
        await conn.commit()

    await init_db(db_path)

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM library_file ORDER BY file_id")
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 2
    a = rows[0]
    assert a["file_id"] == "track_aaa"          # path-hash id becomes file_id
    assert a["current_path"] == "/m/a.flac"
    assert a["size_bytes"] == 111
    assert a["modified_at"] == 1000
    assert a["first_indexed"] == 1710000000     # last_updated is best-effort
    assert a["device_id"] is None               # filled on next scan
    assert a["inode"] is None


@pytest.mark.asyncio
async def test_backfill_idempotent(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                file_path TEXT NOT NULL, format TEXT NOT NULL,
                file_size BIGINT, modified_time INTEGER, last_updated INTEGER
            )
            """
        )
        await conn.execute(
            "INSERT INTO tracks (id, title, file_path, format, file_size, "
            "modified_time, last_updated) VALUES "
            "('track_aaa', 'A', '/m/a.flac', 'audio/flac', 111, 1000, 1710000000)"
        )
        await conn.commit()

    await init_db(db_path)
    # A later scan sets device/inode; a second init_db must not clobber it
    # or duplicate the row.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE library_file SET device_id = '64512', inode = '999' "
            "WHERE file_id = 'track_aaa'"
        )
        await conn.commit()

    await init_db(db_path)

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM library_file")
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["device_id"] == "64512"      # preserved, not reset
    assert rows[0]["inode"] == "999"
