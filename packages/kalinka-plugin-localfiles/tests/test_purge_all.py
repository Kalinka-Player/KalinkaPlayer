"""Tests for ``LocalFilesInputModuleDb.purge_all``.

The "Rebuild library" one-shot must wipe everything derived
from the library — index tables and the whole artwork tree — but preserve
finished CLAP audio embeddings: they depend only on the file bytes and the
model, and track IDs are stable path hashes, so they are snapshotted into
``embedding_snapshot`` for the embedder to re-attach after the rescan. When
the DB can't be cleared in place (not our schema), the file is removed
instead, which is the old full-recompute behaviour.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb


def _db(tmp_path) -> LocalFilesInputModuleDb:
    config = LocalFilesConfig(
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return LocalFilesInputModuleDb(config)


def _write_artwork(db: LocalFilesInputModuleDb) -> None:
    for rel in (
        "album/abc_large.jpg",  # downloaded + procedural album art
        "playlist/p1.jpg",  # playlist collage
        "artist/xyz.jpg",  # enricher / wikidata
    ):
        art = db.artwork_path / rel
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_bytes(b"img")


async def _seed(db_path: str) -> None:
    await init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO artists (id, name) VALUES ('ar1', 'Artist')")
        conn.execute(
            "INSERT INTO albums (id, title, artist_id) VALUES ('al1', 'Album', 'ar1')"
        )
        conn.execute(
            "INSERT INTO tracks (id, title, file_path, format, enriched, artist_id, "
            "album_id, embedding_clap_audio, embedding_version, mood_valence) "
            "VALUES ('t1', 'Embedded', '/m/a.flac', 'flac', 1, 'ar1', 'al1', "
            "X'0102', 4, 0.5)"
        )
        conn.execute(
            "INSERT INTO tracks (id, title, file_path, format, enriched, artist_id, "
            "album_id) VALUES ('t2', 'Not embedded', '/m/b.flac', 'flac', 1, "
            "'ar1', 'al1')"
        )
        conn.execute(
            "INSERT INTO embedding_jobs (entity_type, entity_id, stage, status, "
            "model_version) VALUES ('track', 't1', 'clap_audio', 'done', 4)"
        )
        conn.commit()


@pytest.mark.asyncio
async def test_purge_clears_tables_but_snapshots_audio_embeddings(tmp_path):
    db = _db(tmp_path)
    await _seed(str(db.db_path))
    _write_artwork(db)

    db.purge_all()

    assert db.db_path.exists()
    assert not db.artwork_path.exists()

    with sqlite3.connect(db.db_path) as conn:
        for table in ("tracks", "albums", "artists", "embedding_jobs"):
            assert (
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            ), table
        rows = conn.execute(
            "SELECT track_id, embedding_clap_audio, embedding_version, "
            "mood_valence FROM embedding_snapshot"
        ).fetchall()
    assert rows == [("t1", b"\x01\x02", 4, 0.5)]


@pytest.mark.asyncio
async def test_purge_drops_vec_tables(tmp_path):
    pytest.importorskip("sqlite_vec")
    db = _db(tmp_path)
    await _seed(str(db.db_path))

    db.purge_all()

    with sqlite3.connect(db.db_path) as conn:
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'vec_%'"
        ).fetchall()
    assert leftover == []


def test_purge_falls_back_to_file_removal_on_foreign_schema(tmp_path):
    db = _db(tmp_path)

    for suffix in ("", "-wal", "-shm"):
        Path(str(db.db_path) + suffix).write_bytes(b"x")
    _write_artwork(db)

    db.purge_all()

    for suffix in ("", "-wal", "-shm"):
        assert not Path(str(db.db_path) + suffix).exists()
    assert not db.artwork_path.exists()


def test_purge_all_is_safe_when_nothing_exists(tmp_path):
    # A first-ever boot with the trigger armed must not raise just because the
    # DB and artwork dir don't exist yet.
    db = _db(tmp_path)
    db.purge_all()
    assert not db.db_path.exists()
    assert not db.artwork_path.exists()
