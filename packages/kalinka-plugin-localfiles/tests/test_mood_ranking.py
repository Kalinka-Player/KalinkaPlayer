"""Tests for mood (valence/arousal) ranking: schema migration, DB methods,
query->(V,A) mapping, and the adaptive blend. Hermetic — no ONNX models or
network (the mood index is injected as a fixture)."""

import os
import tempfile
from types import SimpleNamespace

import aiosqlite
import numpy as np
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedding_utils import encode_embedding
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb
from kalinka_plugin_localfiles.searcher import searcher as searcher_mod
from kalinka_plugin_localfiles.searcher.searcher_db import AsyncSearcherDb
from kalinka_plugin_localfiles.searcher.searcher import SearchWorker


def _db_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "test.db")


async def _insert_track(conn, tid, valence=None, arousal=None, blob=None):
    await conn.execute(
        "INSERT INTO tracks (id, title, file_path, format, enriched, "
        "embedding_clap_audio, mood_valence, mood_arousal) "
        "VALUES (?, 'track', 'f', 'mp3', 1, ?, ?, ?)",
        (tid, blob, valence, arousal),
    )


# --- mood index fixture: 3 words on near-orthogonal text embeddings ---
def _fixture_index():
    words = ["melancholic", "energetic", "calm"]
    va = np.array([[2.5, 3.0], [7.0, 8.0], [6.5, 2.5]], dtype=np.float32)
    emb = np.zeros((3, 512), dtype=np.float32)
    emb[0, 0] = 1.0   # melancholic -> e0
    emb[1, 1] = 1.0   # energetic   -> e1
    emb[2, 2] = 1.0   # calm        -> e2
    return [words, va, emb]


def _make_worker(config, db):
    searcher_mod._ensure_numpy()  # set module global np (load is bypassed)
    w = SearchWorker(config, db)
    w._mood_index = _fixture_index()  # inject so _load_mood_index returns it
    return w


class TestHeadVersionMigration:
    @pytest.mark.asyncio
    async def test_version_bump_clears_stale_mood_on_startup(self):
        from kalinka_plugin_localfiles.embedding_utils import VA_HEAD_VERSION

        db_path = _db_path()
        await init_db(db_path)  # records va_head = current version
        async with aiosqlite.connect(db_path) as conn:
            await _insert_track(conn, "t0", 5.0, 5.0)  # has (stale) mood
            # Simulate a DB built by a prior head (no/old version recorded).
            await conn.execute(
                "DELETE FROM embedding_model_versions WHERE model_name='va_head'"
            )
            await conn.commit()

        await init_db(db_path)  # startup migration should detect + clear

        async with aiosqlite.connect(db_path) as conn:
            cur = await conn.execute("SELECT mood_valence FROM tracks WHERE id='t0'")
            assert (await cur.fetchone())[0] is None  # cleared for recompute
            cur = await conn.execute(
                "SELECT version FROM embedding_model_versions WHERE model_name='va_head'"
            )
            assert (await cur.fetchone())[0] == VA_HEAD_VERSION

    @pytest.mark.asyncio
    async def test_same_version_keeps_mood_idempotent(self):
        db_path = _db_path()
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as conn:
            await _insert_track(conn, "t0", 5.0, 6.0)
            await conn.commit()
        await init_db(db_path)  # version unchanged -> must NOT clear
        async with aiosqlite.connect(db_path) as conn:
            cur = await conn.execute("SELECT mood_valence FROM tracks WHERE id='t0'")
            assert (await cur.fetchone())[0] == 5.0


class TestSchemaMigration:
    @pytest.mark.asyncio
    async def test_alter_adds_mood_columns_to_existing_db(self):
        db_path = _db_path()
        # Pre-existing DB whose tracks table predates the mood columns.
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE tracks (id TEXT PRIMARY KEY, title TEXT, "
                "file_path TEXT, format TEXT, embedding_clap_audio BLOB, "
                "embedding_clap_text BLOB)"
            )
            await conn.commit()
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(tracks)")
            cols = {r[1] for r in await cur.fetchall()}
        assert {"mood_valence", "mood_arousal"} <= cols


class TestMoodDbMethods:
    @pytest.mark.asyncio
    async def test_knn_search_mood_sorted_by_proximity(self):
        db_path = _db_path()
        config = LocalFilesConfig(db_path=db_path)
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as conn:
            await _insert_track(conn, "sad", 2.0, 2.5)
            await _insert_track(conn, "happy", 8.0, 7.5)
            await _insert_track(conn, "mid", 5.0, 5.0)
            await _insert_track(conn, "no_va", None, None)  # excluded
            await conn.commit()
        db = AsyncSearcherDb(config)

        hits = await db.knn_search_mood(2.0, 2.5, 50)  # target near "sad"
        ids = [h["track_id"] for h in hits]
        assert ids[0] == "sad"
        assert "no_va" not in ids  # tracks without VA are excluded
        assert hits == sorted(hits, key=lambda h: h["distance"])

    @pytest.mark.asyncio
    async def test_get_tracks_va_bulk(self):
        db_path = _db_path()
        config = LocalFilesConfig(db_path=db_path)
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as conn:
            await _insert_track(conn, "a", 3.0, 4.0)
            await _insert_track(conn, "b", None, None)
            await conn.commit()
        db = AsyncSearcherDb(config)
        va = await db.get_tracks_va_bulk(["a", "b", "missing"])
        assert va == {"a": (3.0, 4.0)}


class TestEmbedderBackfill:
    @pytest.mark.asyncio
    async def test_needing_va_and_store_roundtrip(self):
        db_path = _db_path()
        config = LocalFilesConfig(db_path=db_path)
        await init_db(db_path)
        blob = encode_embedding(np.ones(512, dtype=np.float32) / np.sqrt(512))
        async with aiosqlite.connect(db_path) as conn:
            await _insert_track(conn, "t0", None, None, blob=blob)   # needs VA
            await _insert_track(conn, "t1", 5.0, 5.0, blob=blob)     # already has
            await _insert_track(conn, "t2", None, None, blob=None)   # no embedding
            await conn.commit()
        db = AsyncEmbedderDb(config)

        need = await db.get_tracks_needing_va(100)
        assert [tid for tid, _ in need] == ["t0"]  # only embedded + missing VA
        await db.store_mood_va([("t0", 4.2, 5.5)])
        assert await db.get_tracks_needing_va(100) == []


class TestMoodIndexLoad:
    def test_loads_on_a_freshly_booted_pi(self, tmp_path, monkeypatch):
        words, va, emb = _fixture_index()
        path = tmp_path / "mood_index.npz"
        np.savez(path, words=np.array(words), va=va, text_emb=emb)
        monkeypatch.setattr(
            "kalinka_plugin_localfiles.embedder.clap_onnx._ensure_model_file",
            lambda _name, _model_dir: str(path),
        )
        monkeypatch.setattr(
            searcher_mod, "time", SimpleNamespace(monotonic=lambda: 175.0)
        )
        config = LocalFilesConfig(db_path=_db_path())
        w = SearchWorker(config, AsyncSearcherDb(config))

        index = w._load_mood_index()

        assert index is not None and index[0] == words


class TestQueryToVa:
    @pytest.mark.asyncio
    async def test_keyword_match_full_confidence(self):
        config = LocalFilesConfig(db_path=_db_path())
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        (tva, conf) = w._query_to_va("something melancholic for tonight", None)
        assert conf == 1.0
        assert tva == (2.5, 3.0)

    @pytest.mark.asyncio
    async def test_mixed_query_backs_off(self):
        # "melancholic piano": 1 of 2 content words is a mood word -> conf 0.5,
        # so the CLAP leg keeps the "piano" signal instead of mood dominating.
        config = LocalFilesConfig(db_path=_db_path())
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        (tva, conf) = w._query_to_va("melancholic piano", None)
        assert tva == (2.5, 3.0)
        assert conf == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_filler_words_dont_dilute_pure_mood(self):
        # Fillers ("something", "for", "tonight") don't count as content, so a
        # pure-mood query keeps full confidence.
        config = LocalFilesConfig(db_path=_db_path())
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        (_, conf) = w._query_to_va("something melancholic for tonight", None)
        assert conf == 1.0

    @pytest.mark.asyncio
    async def test_nn_fallback_above_threshold(self):
        config = LocalFilesConfig(db_path=_db_path())
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        # Query blob aligned with the "energetic" word embedding -> cos ~1.
        q = np.zeros(512, dtype=np.float32)
        q[1] = 1.0
        (tva, conf) = w._query_to_va("intense gym session", encode_embedding(q))
        assert tva == (7.0, 8.0)  # picked "energetic"
        assert conf > 0.9

    @pytest.mark.asyncio
    async def test_nn_threshold_one_no_div_by_zero(self):
        # nn_threshold == 1.0 with an exact-cosine match must not divide by zero.
        config = LocalFilesConfig(db_path=_db_path())
        config.ai_search.mood.nn_threshold = 1.0
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        q = np.zeros(512, dtype=np.float32)
        q[1] = 1.0  # exact match to "energetic" -> top_cos == 1.0 == threshold
        (tva, conf) = w._query_to_va("gym", encode_embedding(q))
        assert tva == (7.0, 8.0) and conf == 1.0

    @pytest.mark.asyncio
    async def test_empty_mood_index_no_indexerror(self):
        # A loaded-but-empty index must not IndexError on the NN path.
        config = LocalFilesConfig(db_path=_db_path())
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        w._mood_index = ([], np.zeros((0, 2), np.float32), np.zeros((0, 512), np.float32))
        q = np.zeros(512, dtype=np.float32); q[0] = 1.0
        assert w._query_to_va("anything", encode_embedding(q)) == (None, 0.0)

    @pytest.mark.asyncio
    async def test_nn_below_threshold_is_non_mood(self):
        config = LocalFilesConfig(db_path=_db_path())
        config.ai_search.mood.nn_threshold = 0.5
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        # Orthogonal to every mood word -> cos 0 < threshold -> no mood.
        q = np.zeros(512, dtype=np.float32)
        q[300] = 1.0
        (tva, conf) = w._query_to_va("piano sonata in c", encode_embedding(q))
        assert tva is None and conf == 0.0

    @pytest.mark.asyncio
    async def test_nn_fallback_disabled(self):
        config = LocalFilesConfig(db_path=_db_path())
        config.ai_search.mood.nn_fallback = False
        db = AsyncSearcherDb(config)
        w = _make_worker(config, db)
        q = np.zeros(512, dtype=np.float32)
        q[1] = 1.0
        (tva, conf) = w._query_to_va("gym session", encode_embedding(q))
        assert tva is None and conf == 0.0  # keyword-only mode
