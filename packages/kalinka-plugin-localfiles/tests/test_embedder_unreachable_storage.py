"""Embedding a library whose storage is not always there.

A Pi embeds a large library over days, and a NAS sleeps, reboots and loses
power in that time. A job spent on a share that did not answer was spent for
good: three such passes and every track the outage touched was given up on,
with nothing that would ever queue it again.
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import BinaryIO

import aiosqlite
import numpy as np
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb
from kalinka_plugin_localfiles.storage import (
    DirEntry,
    FileStat,
    FileStorage,
    RootStatus,
    StorageResolver,
    is_within,
    scheme_of,
)

ROOT = "shelf://nas/music"
OTHER = "shelf://vault/music"


class ShelfStorage(FileStorage):
    """Shares that can be switched off one server at a time, or made to
    refuse one file."""

    def __init__(self) -> None:
        super().__init__()
        self.offline: set[str] = set()
        self.refused: set[str] = set()
        self.probes = 0

    @property
    def online(self) -> bool:
        return not self.offline

    @online.setter
    def online(self, value: bool) -> None:
        self.offline = set() if value else {ROOT, OTHER}

    def _down(self, path: str) -> bool:
        return any(is_within(path, root) for root in self.offline)

    @property
    def scheme(self) -> str:
        return "shelf"

    def handles(self, path: str) -> bool:
        return scheme_of(path) == "shelf"

    def canonical(self, path: str) -> str:
        return path.rstrip("/")

    def contains(self, path: str, roots) -> bool:
        return any(is_within(path, root) for root in roots)

    def listdir(self, path: str) -> list[DirEntry]:
        return []

    def stat(self, path: str) -> FileStat:
        return FileStat(size=5, mtime_ns=0, is_dir=False)

    def open(self, path: str) -> BinaryIO:
        if self._down(path):
            raise OSError("nas did not answer for share 'music'")
        if path in self.refused:
            raise PermissionError(f"'media' may not read {path}")
        return io.BytesIO(b"audio")

    def probe_root_blocking(self, root: str) -> RootStatus:
        self.probes += 1
        if self._down(root):
            return self.unavailable(root, "nas did not answer for share 'music'")
        return RootStatus(
            root=root,
            available=True,
            reason="",
            fs_type="shelf",
            is_network=True,
            is_autofs=False,
        )


class _Encoder:
    """Stands in for CLAP: reads the stream, as the real one does."""

    def __init__(self, fails_on: bytes = b"") -> None:
        self.fails_on = fails_on

    def get_audio_embedding(self, audio):
        data = audio.read()
        if self.fails_on and data == self.fails_on:
            raise ValueError("not a decodable stream")
        return np.ones(512, dtype="float32")


async def _worker(tracks: list[str], max_attempts: int = 3, batch_size: int = 8):
    db_path = os.path.join(tempfile.mkdtemp(), "library.db")
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as conn:
        for name in tracks:
            await conn.execute(
                "INSERT INTO tracks (id, title, file_path, format, enriched) "
                "VALUES (?, ?, ?, 'flac', 0)",
                (name, name, name if "://" in name else f"{ROOT}/{name}.flac"),
            )
        await conn.commit()
    config = LocalFilesConfig(music_folders=[ROOT, OTHER], db_path=db_path)
    config.ai_search.max_job_attempts = max_attempts
    config.ai_search.audio_batch_size = batch_size
    db = AsyncEmbedderDb(config)
    await db.schedule_new_jobs(clap_version=1)
    worker = EmbeddingWorker(config, db)
    shelf = ShelfStorage()
    worker.storage = StorageResolver([shelf])
    worker._clap = _Encoder()
    worker._audio_available = True
    return worker, shelf, db


async def _jobs(db) -> dict[str, tuple[str, int]]:
    async with aiosqlite.connect(db.db_path) as conn:
        cursor = await conn.execute(
            "SELECT entity_id, status, attempts FROM embedding_jobs "
            "WHERE stage = 'clap_audio'"
        )
        return {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_a_share_that_does_not_answer_spends_no_attempts():
    worker, shelf, db = await _worker(["a", "b", "c"])
    shelf.online = False

    assert await worker._process_clap_batch() is False
    await db.resume_deferred_jobs()
    worker._unreachable.clear()
    assert await worker._process_clap_batch() is False

    assert await _jobs(db) == {name: ("deferred", 0) for name in "abc"}


@pytest.mark.asyncio
async def test_a_share_that_does_not_answer_is_asked_once_a_pass():
    worker, shelf, db = await _worker(["a", "b", "c"])
    shelf.online = False

    await worker._process_clap_batch()

    assert shelf.probes == 1


@pytest.mark.asyncio
async def test_the_rest_of_the_library_is_not_held_up_behind_it():
    """The claim takes the oldest jobs first, so a down share's tracks handed
    back as pending would head every batch until it returned."""
    shares = [f"{ROOT}/{n}.flac" for n in "abcd"]
    others = [f"{OTHER}/{n}.flac" for n in "wxyz"]
    worker, shelf, db = await _worker(shares + others, batch_size=2)
    shelf.offline = {ROOT}

    while await db.has_pending_jobs("clap_audio"):
        await worker._process_clap_batch()

    jobs = await _jobs(db)
    assert {jobs[path][0] for path in others} == {"done"}
    assert {jobs[path] for path in shares} == {("deferred", 0)}


@pytest.mark.asyncio
async def test_the_tracks_are_embedded_once_it_answers_again():
    worker, shelf, db = await _worker(["a", "b"])
    shelf.online = False
    await worker._process_clap_batch()

    shelf.online = True
    await db.resume_deferred_jobs()
    worker._unreachable.clear()
    assert await worker._process_clap_batch() is True

    assert {status for status, _ in (await _jobs(db)).values()} == {"done"}


@pytest.mark.asyncio
async def test_one_unreadable_file_on_a_live_share_fails_on_its_own():
    """Released instead, it would be claimed again at the head of every
    batch and hold the whole queue up behind it."""
    worker, shelf, db = await _worker(["a", "b", "c"], max_attempts=1)
    shelf.refused.add(f"{ROOT}/b.flac")

    await worker._process_clap_batch()

    jobs = await _jobs(db)
    assert jobs["b"][0] == "failed"
    assert jobs["a"][0] == jobs["c"][0] == "done"


@pytest.mark.asyncio
async def test_audio_that_will_not_decode_still_counts_against_its_job():
    worker, shelf, db = await _worker(["a"], max_attempts=1)
    worker._clap = _Encoder(fails_on=b"audio")

    await worker._process_clap_batch()

    assert (await _jobs(db))["a"][0] == "failed"
