"""The embedding pipeline's ordering contract.

clap_audio depends only on the file, so it is scheduled the moment the
indexer lands a track — semantic search must not wait hours for MusicBrainz.
clap_text embeds metadata and the aggregates pool by album/artist membership,
both of which the enricher may still change, so those two wait for its
verdict (ENRICHED or FAILED).
"""

from __future__ import annotations

import os
import tempfile

import aiosqlite
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb


async def _seed(track_rows) -> AsyncEmbedderDb:
    db_path = os.path.join(tempfile.mkdtemp(), "test.db")
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as conn:
        for tid, enriched in track_rows:
            await conn.execute(
                "INSERT INTO tracks (id, title, file_path, format, enriched) "
                "VALUES (?, ?, ?, 'flac', ?)",
                (tid, tid, f"/m/{tid}.flac", enriched),
            )
        await conn.commit()
    return AsyncEmbedderDb(LocalFilesConfig(db_path=db_path))


async def _jobs(db, stage):
    async with aiosqlite.connect(db.db_path) as conn:
        cursor = await conn.execute(
            "SELECT entity_id FROM embedding_jobs WHERE stage = ?", (stage,)
        )
        return {row[0] for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_audio_is_scheduled_before_any_enrichment():
    """A freshly indexed library queues for audio embedding immediately."""
    db = await _seed([("t1", 0), ("t2", 0)])

    await db.schedule_new_jobs(clap_version=1)

    assert await _jobs(db, "clap_audio") == {"t1", "t2"}
    assert await _jobs(db, "clap_text") == set()


@pytest.mark.asyncio
async def test_text_follows_the_enrichment_verdict():
    """The text job appears once the enricher rules — ENRICHED or FAILED."""
    db = await _seed([("t1", 0), ("t2", 0), ("t3", 0)])
    await db.schedule_new_jobs(clap_version=1)

    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute("UPDATE tracks SET enriched = 1 WHERE id = 't1'")
        await conn.execute("UPDATE tracks SET enriched = 2 WHERE id = 't2'")
        await conn.commit()
    await db.schedule_new_jobs(clap_version=1)

    assert await _jobs(db, "clap_text") == {"t1", "t2"}


@pytest.mark.asyncio
async def test_filter_enriched_tracks_returns_only_ruled_rows():
    db = await _seed([("t1", 1), ("t2", 0), ("t3", 2)])

    settled = await db.filter_enriched_tracks(["t1", "t2", "t3", "ghost"])

    assert sorted(settled) == ["t1", "t3"]
    assert await db.filter_enriched_tracks([]) == []


@pytest.mark.asyncio
async def test_audio_completion_pools_aggregates_only_for_settled_tracks():
    """Audio finishing on a pre-verdict track must not pool aggregates —
    clustering may still re-point its album; its text job covers it later."""

    class Db:
        def __init__(self):
            self.completed: list[str] = []
            self._batch = [
                {"id": 1, "entity_id": "settled", "model_version": 1},
                {"id": 2, "entity_id": "pending", "model_version": 1},
            ]

        async def claim_batch(self, stage, limit):
            batch, self._batch = self._batch, []
            return batch

        async def get_file_path_for_track(self, track_id):
            return f"/m/{track_id}.flac"

        async def complete_clap_job(self, job_id, track_id, blob, version):
            self.completed.append(track_id)

        async def fail_job(self, job_id, reason, max_attempts):
            raise AssertionError(f"unexpected failure: {reason}")

        async def filter_enriched_tracks(self, ids):
            return [i for i in ids if i == "settled"]

    aggregated: list[list[str]] = []

    async def record(ids):
        aggregated.append(list(ids))

    worker = EmbeddingWorker.__new__(EmbeddingWorker)
    worker.db = Db()
    worker.config = LocalFilesConfig(db_path="unused")
    worker._unreachable = {}
    worker._audio_available = True
    worker._compute_clap_audio = lambda path: b"\x01"
    worker._update_aggregate_embeddings = record

    assert await worker._process_clap_batch() is True

    assert worker.db.completed == ["settled", "pending"]
    assert aggregated == [["settled"]]
