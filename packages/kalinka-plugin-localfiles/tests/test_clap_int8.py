"""CLAP int8 vector quantization: round-trip, storage format, migration.

The embedder stores CLAP vectors as symmetric int8 (4x smaller than float32)
instead of raw float32. These tests cover:

  * encode/decode round-trip fidelity and the 512-byte int8 footprint,
  * the float32 -> int8 schema migration (drop typed vec tables, clear blobs,
    stamp PRAGMA user_version) on an existing legacy database,
  * the write + KNN-search path against the int8 vec0 tables,
  * the backward-compat guarantee that an int8 query against a not-yet-migrated
    float database returns no results rather than crashing.
"""

import os
import tempfile
import time

import aiosqlite
import numpy as np
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb
from kalinka_plugin_localfiles.embedding_utils import (
    CLAP_EMBED_FORMAT_VERSION,
    CLAP_INT8_CAP,
    decode_embedding,
    encode_embedding,
    normalise,
)
from kalinka_plugin_localfiles.searcher.searcher_db import AsyncSearcherDb

sqlite_vec = pytest.importorskip("sqlite_vec")

DIM = 512


def _unit_vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return normalise(rng.standard_normal(DIM).astype(np.float32))


def _config(db_path: str) -> LocalFilesConfig:
    return LocalFilesConfig(db_path=db_path)


# ---------------------------------------------------------------------------
# Pure quantization
# ---------------------------------------------------------------------------


def test_int8_roundtrip_is_512_bytes_and_high_fidelity():
    v = _unit_vec(1)
    blob = encode_embedding(v)
    assert len(blob) == DIM  # one int8 per dimension, 4x smaller than float32
    back = decode_embedding(blob)
    cos = float(np.dot(normalise(back), v))
    assert cos > 0.999  # symmetric int8 preserves direction near-perfectly


def test_encode_saturates_out_of_range_components():
    # Components beyond the cap saturate at +/-127 instead of wrapping.
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = 10.0  # far above CAP
    v[1] = -10.0
    q = np.frombuffer(encode_embedding(v), dtype=np.int8)
    assert q[0] == 127 and q[1] == -127


def test_cap_covers_observed_range():
    # Sanity: the cap leaves headroom over a realistic unit-vector component.
    assert CLAP_INT8_CAP >= 0.23


# ---------------------------------------------------------------------------
# Migration from the legacy float32 format
# ---------------------------------------------------------------------------


async def _make_legacy_float_db(db_path: str) -> None:
    """Build a database in the pre-int8 (float[512]) on-disk format."""
    await init_db(db_path)  # creates current (int8) schema...
    # ...then rewrite the vec tables as float[512] and seed legacy state, so the
    # next init_db sees a v0/float database to migrate.
    async with aiosqlite.connect(db_path) as conn:
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        await conn.enable_load_extension(False)
        await conn.execute("DROP TABLE IF EXISTS vec_tracks_clap")
        await conn.execute(
            f"CREATE VIRTUAL TABLE vec_tracks_clap USING vec0("
            f"track_id TEXT PRIMARY KEY, embedding float[{DIM}])"
        )
        fblob = _unit_vec(7).tobytes()  # 2048-byte float32 blob
        await conn.execute(
            "INSERT INTO vec_tracks_clap(track_id, embedding) VALUES('t1', ?)",
            (fblob,),
        )
        await conn.execute(
            "INSERT INTO tracks (id, title, file_path, format, enriched, "
            "embedding_clap_audio, embedding_version) "
            "VALUES ('t1', 'Song', 'f1', 'mp3', 1, ?, 3)",
            (fblob,),
        )
        await conn.execute("PRAGMA user_version = 0")
        await conn.commit()


@pytest.mark.asyncio
async def test_migration_rebuilds_int8_and_clears_blobs():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        await _make_legacy_float_db(db)

        # Precondition: legacy float format with a stored embedding.
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='vec_tracks_clap'"
            )
            assert "float[512]" in (await cur.fetchone())[0]

        # Restart with the current code.
        await init_db(db)

        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == CLAP_EMBED_FORMAT_VERSION
            cur = await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='vec_tracks_clap'"
            )
            assert "int8[512]" in (await cur.fetchone())[0]
            # Stale blobs cleared so the embedder recomputes them in int8 and
            # aggregates never mix formats.
            cur = await conn.execute(
                "SELECT embedding_clap_audio, embedding_version FROM tracks WHERE id='t1'"
            )
            row = await cur.fetchone()
            assert row[0] is None and row[1] == 0

        # Idempotent: a second restart is a no-op.
        await init_db(db)
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == CLAP_EMBED_FORMAT_VERSION


# ---------------------------------------------------------------------------
# Write + search round-trip on the int8 vec tables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_then_knn_search_int8():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "fresh.db")
        await init_db(db)
        cfg = _config(db)

        async with aiosqlite.connect(db) as conn:
            for i in range(5):
                await conn.execute(
                    "INSERT INTO tracks (id, title, file_path, format, enriched) "
                    "VALUES (?, ?, ?, 'mp3', 1)",
                    (f"t{i}", f"Song {i}", f"f{i}"),
                )
            await conn.commit()

        edb = AsyncEmbedderDb(cfg)
        await edb._check_vec_available()
        vecs = {f"t{i}": _unit_vec(100 + i) for i in range(5)}
        for tid, v in vecs.items():
            # job_id 0 is fine; the embedding_jobs row update is a no-op here.
            await edb.complete_clap_job(0, tid, encode_embedding(v), version=4)

        sdb = AsyncSearcherDb(cfg)
        await sdb._check_vec_available()
        # Self-query: the matching track must come back first at distance 0.
        res = await sdb.knn_search_audio(encode_embedding(vecs["t2"]), limit=5)
        assert res, "expected KNN hits from the int8 vec table"
        assert res[0]["track_id"] == "t2"
        assert res[0]["distance"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_int8_query_against_float_db_returns_empty():
    """A not-yet-migrated float database must not crash an int8 query."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        await _make_legacy_float_db(db)  # float vec tables, NOT migrated

        sdb = AsyncSearcherDb(_config(db))
        await sdb._check_vec_available()
        res = await sdb.knn_search_audio(encode_embedding(_unit_vec(7)), limit=5)
        assert res == []  # type mismatch is swallowed -> no results, no crash


@pytest.mark.asyncio
async def test_coverage_counts_only_latest_version():
    """A version bump leaves superseded jobs behind; coverage must report one
    row per track (latest model_version), not sum across versions."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "cov.db")
        await init_db(db)
        async with aiosqlite.connect(db) as conn:
            for i in range(3):
                await conn.execute(
                    "INSERT INTO tracks (id, title, file_path, format, enriched) "
                    "VALUES (?, ?, ?, 'mp3', 1)",
                    (f"t{i}", f"Song {i}", f"f{i}"),
                )
            # Old (superseded) v3 jobs, all done, plus current v4 jobs mid-recompute.
            for i in range(3):
                await conn.execute(
                    "INSERT INTO embedding_jobs (entity_type, entity_id, stage, "
                    "status, model_version) VALUES ('track', ?, 'clap_audio', 'done', 3)",
                    (f"t{i}",),
                )
            for i, status in enumerate(["done", "done", "pending"]):
                await conn.execute(
                    "INSERT INTO embedding_jobs (entity_type, entity_id, stage, "
                    "status, model_version) VALUES ('track', ?, 'clap_audio', ?, 4)",
                    (f"t{i}", status),
                )
            await conn.commit()

        cov = await AsyncEmbedderDb(_config(db)).get_embedding_coverage()
        audio = cov["clap_audio"]
        assert audio["total"] == 3  # not 6 — only the latest version is counted
        assert audio["done"] == 2 and audio["pending"] == 1
        assert audio["coverage_pct"] == pytest.approx(66.7, abs=0.1)


async def _large_library(db: str, n: int, missing_audio_jobs: int = 0) -> None:
    await init_db(db)
    async with aiosqlite.connect(db) as conn:
        await conn.executemany(
            "INSERT INTO tracks (id, title, file_path, format, enriched) "
            "VALUES (?, ?, ?, 'mp3', 1)",
            ((f"t{i}", f"Song {i}", f"f{i}") for i in range(n)),
        )
        await conn.executemany(
            "INSERT INTO embedding_jobs (entity_type, entity_id, stage, "
            "status, model_version) VALUES ('track', ?, ?, 'pending', 1)",
            (
                (f"t{i}", stage)
                for stage in ("clap_audio", "clap_text")
                for i in range(n)
                if stage != "clap_audio" or i >= missing_audio_jobs
            ),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_coverage_stays_fast_on_a_large_job_table():
    """The MAX-per-stage subquery must stay indexed, or coverage goes O(n²)
    and every get_indexer_status call stalls for ~40s on a 16k library."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "cov_large.db")
        n = 10_000
        await _large_library(db, n)
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='embedding_jobs'"
            )
            indexes = {r[0] for r in await cur.fetchall()}
            assert "idx_jobs_stage_version" in indexes

        start = time.monotonic()
        cov = await AsyncEmbedderDb(_config(db)).get_embedding_coverage()
        elapsed = time.monotonic() - start
        assert cov["clap_audio"]["pending"] == n
        assert cov["clap_text"]["pending"] == n
        # ~20ms when indexed; seconds-to-minutes when quadratic.
        assert elapsed < 2.0, f"coverage query took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_scheduling_stays_fast_on_a_large_job_table():
    """Scheduling holds the write lock while it runs: probing the queued jobs
    of each track took minutes on a 19k-track Pi, and every other writer of
    the library failed as locked meanwhile."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "schedule_large.db")
        await _large_library(db, 20_000, missing_audio_jobs=3)

        start = time.monotonic()
        inserted = await AsyncEmbedderDb(_config(db)).schedule_new_jobs(1)
        elapsed = time.monotonic() - start
        assert inserted == 3
        # ~20ms through the unique key; seconds when quadratic.
        assert elapsed < 2.0, f"scheduling took {elapsed:.1f}s"
