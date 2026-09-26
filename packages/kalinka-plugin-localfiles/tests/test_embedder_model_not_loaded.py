"""Embedding while a CLAP model is not loaded.

``time.monotonic()`` counts from boot. An embedder nudged awake within its
first poll interval of a Pi booting read "never tried to load the audio
model" as "tried just now", skipped the load, and failed its whole audio
backlog for good — every claimed job came back "clap returned None".
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import time
from types import SimpleNamespace

import aiosqlite
import numpy as np
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder import embedder as embedder_module
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb
from kalinka_plugin_localfiles.embedding_utils import CLAP_MODEL_VERSION

SECONDS_SINCE_BOOT = 175.0


class _FakeClap:
    _model_dir = "/fake/models"
    has_va_head = False

    def __init__(self) -> None:
        self.is_text_loaded = False
        self.is_audio_loaded = False

    def load_text(self) -> None:
        self.is_text_loaded = True

    def load_audio(self) -> None:
        self.is_audio_loaded = True

    def unload_audio(self) -> None:
        self.is_audio_loaded = False

    def get_text_embedding(self, _text):
        return np.ones(512, dtype="float32")

    def get_audio_embedding(self, audio):
        audio.read()
        return np.ones(512, dtype="float32")


async def _library(tmp_path, names, enriched=0):
    music = tmp_path / "music"
    music.mkdir()
    db_path = str(tmp_path / "library.db")
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as conn:
        for name in names:
            path = music / f"{name}.flac"
            path.write_bytes(b"audio")
            await conn.execute(
                "INSERT INTO tracks (id, title, file_path, format, enriched) "
                "VALUES (?, ?, ?, 'flac', ?)",
                (name, name, str(path), enriched),
            )
        await conn.commit()
    config = LocalFilesConfig(music_folders=[str(music)], db_path=db_path)
    return config, AsyncEmbedderDb(config)


def _worker(config, db) -> EmbeddingWorker:
    worker = EmbeddingWorker(config, db)
    clap = _FakeClap()

    def _new_clap():
        worker._clap = clap
        return clap

    worker._new_clap = _new_clap
    return worker


async def _jobs(db, stage) -> dict[str, tuple[str, int]]:
    async with aiosqlite.connect(db.db_path) as conn:
        cursor = await conn.execute(
            "SELECT entity_id, status, attempts FROM embedding_jobs WHERE stage = ?",
            (stage,),
        )
        return {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}


async def _until_audio_settles(db, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = await _jobs(db, "clap_audio")
        if jobs and all(s not in ("pending", "in_progress") for s, _ in jobs.values()):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"audio jobs never settled: {await _jobs(db, 'clap_audio')}")


@pytest.mark.asyncio
async def test_an_embedder_woken_soon_after_boot_embeds_the_audio(
    tmp_path, monkeypatch
):
    config, db = await _library(tmp_path, ["a", "b", "c"])
    worker = _worker(config, db)
    start = time.monotonic()

    def since_boot() -> float:
        return SECONDS_SINCE_BOOT + time.monotonic() - start

    monkeypatch.setattr(embedder_module, "time", SimpleNamespace(monotonic=since_boot))
    monkeypatch.setattr(embedder_module, "_ensure_package", lambda _name: True)
    assert config.ai_search.poll_interval_seconds > SECONDS_SINCE_BOOT

    shutdown = asyncio.Event()
    nudges: queue.Queue = queue.Queue()
    nudges.put_nowait("nudge")
    task = asyncio.create_task(worker.run(shutdown, nudges))
    try:
        await _until_audio_settles(db)
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=5)

    assert await _jobs(db, "clap_audio") == {n: ("done", 1) for n in "abc"}


@pytest.mark.asyncio
async def test_audio_jobs_wait_for_the_model_instead_of_failing(tmp_path):
    config, db = await _library(tmp_path, ["a", "b"])
    config.ai_search.max_job_attempts = 1
    await db.schedule_new_jobs(CLAP_MODEL_VERSION)
    worker = _worker(config, db)

    assert await worker._process_clap_batch() is False

    assert await _jobs(db, "clap_audio") == {"a": ("pending", 0), "b": ("pending", 0)}


@pytest.mark.asyncio
async def test_text_jobs_wait_for_the_model_instead_of_failing(tmp_path):
    config, db = await _library(tmp_path, ["a"], enriched=1)
    config.ai_search.max_job_attempts = 1
    await db.schedule_new_jobs(CLAP_MODEL_VERSION)
    worker = _worker(config, db)

    assert await worker._process_clap_text_batch() is False

    assert await _jobs(db, "clap_text") == {"a": ("pending", 0)}


@pytest.mark.asyncio
async def test_audio_jobs_an_earlier_build_gave_up_on_are_requeued(tmp_path):
    """Those builds failed a job the same way whether its audio would not
    decode or the model had never loaded, so none of them can be trusted."""
    config, db = await _library(tmp_path, ["lost", "unreadable"], enriched=1)
    await db.schedule_new_jobs(CLAP_MODEL_VERSION)
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute(
            "UPDATE embedding_jobs SET status = 'failed', attempts = 3, "
            "error = 'clap returned None' "
            "WHERE entity_id = 'lost' AND stage = 'clap_audio'"
        )
        await conn.execute(
            "UPDATE embedding_jobs SET status = 'failed', attempts = 3, "
            "error = 'unreadable: gone' "
            "WHERE entity_id = 'unreadable' AND stage = 'clap_audio'"
        )
        await conn.execute(
            "UPDATE embedding_jobs SET status = 'failed', attempts = 3, "
            "error = 'clap text returned None' WHERE stage = 'clap_text'"
        )
        await conn.commit()

    assert await db.requeue_ambiguous_audio_failures(CLAP_MODEL_VERSION) == 1
    assert await db.requeue_ambiguous_audio_failures(CLAP_MODEL_VERSION) == 0

    assert await _jobs(db, "clap_audio") == {
        "lost": ("pending", 0),
        "unreadable": ("failed", 3),
    }
    assert {s for s, _ in (await _jobs(db, "clap_text")).values()} == {"failed"}


@pytest.mark.asyncio
async def test_requeue_leaves_older_generations_and_removed_tracks_alone(tmp_path):
    config, db = await _library(tmp_path, ["kept"])
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.executemany(
            "INSERT INTO embedding_jobs "
            "(entity_type, entity_id, stage, status, model_version, attempts, error) "
            "VALUES ('track', ?, 'clap_audio', 'failed', ?, 3, 'clap returned None')",
            [
                ("kept", CLAP_MODEL_VERSION - 1),
                ("removed", CLAP_MODEL_VERSION),
            ],
        )
        await conn.commit()

    assert await db.requeue_ambiguous_audio_failures(CLAP_MODEL_VERSION) == 0

    assert await _jobs(db, "clap_audio") == {
        "kept": ("failed", 3),
        "removed": ("failed", 3),
    }


@pytest.mark.asyncio
async def test_audio_that_will_not_embed_now_stays_given_up_on(tmp_path, caplog):
    config, db = await _library(tmp_path, ["a"])
    config.ai_search.max_job_attempts = 1
    await db.schedule_new_jobs(CLAP_MODEL_VERSION)
    worker = _worker(config, db)
    worker._new_clap().get_audio_embedding = lambda _audio: None
    worker._audio_available = True

    with caplog.at_level(logging.WARNING):
        await worker._process_clap_batch()

    assert await db.requeue_ambiguous_audio_failures(CLAP_MODEL_VERSION) == 0
    assert await _jobs(db, "clap_audio") == {"a": ("failed", 1)}
    assert "Gave up on clap_audio" in caplog.text
