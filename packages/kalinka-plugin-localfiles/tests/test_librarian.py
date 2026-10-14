#!/usr/bin/env python3
"""The librarian (Phase 2b) runs the indexer and enricher worker loops in one
process, wired by an in-process asyncio queue instead of a cross-process
multiprocessing queue. These cover the handoff contract and that the
orchestrator points both worker modules at the same queue object.
"""

import asyncio

import pytest
import pytest_asyncio

from kalinka_plugin_localfiles import librarian
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer import indexer as indexer_mod
from kalinka_plugin_localfiles.enricher import enricher as enricher_mod


@pytest.mark.asyncio
async def test_trigger_enricher_update_puts_on_shared_queue():
    q: asyncio.Queue = asyncio.Queue()
    indexer_mod._enricher_queue = q
    try:
        await indexer_mod.trigger_enricher_update("enrich")
        assert q.get_nowait() == "enrich"
    finally:
        indexer_mod._enricher_queue = None


@pytest.mark.asyncio
async def test_trigger_enricher_update_noop_without_queue():
    indexer_mod._enricher_queue = None
    # No queue wired (e.g. a unit test exercising FileIndexer alone): the
    # trigger is a silent no-op, not an error.
    await indexer_mod.trigger_enricher_update("enrich")


@pytest_asyncio.fixture
async def config(tmp_path):
    cfg = LocalFilesConfig(
        music_folders=[str(tmp_path / "music")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    (tmp_path / "music").mkdir()
    await init_db(cfg.db_path)
    return cfg


@pytest.mark.asyncio
async def test_librarian_wires_one_queue_into_both_workers(config, monkeypatch):
    # Don't run the real worker loops — just observe the wiring async_main does.
    monkeypatch.setattr(indexer_mod, "start_indexer", lambda c, db, scan_control=None: None)
    monkeypatch.setattr(indexer_mod, "start_file_watcher", lambda c: None)
    monkeypatch.setattr(enricher_mod, "start_enricher", lambda c, db: None)

    task = asyncio.create_task(librarian.async_main(config, None))
    try:
        await asyncio.sleep(0.05)
        assert isinstance(indexer_mod._enricher_queue, asyncio.Queue)
        # Same object on both sides — the whole point of the merge.
        assert indexer_mod._enricher_queue is enricher_mod._enricher_queue
        assert indexer_mod._shutdown_event is enricher_mod._shutdown_event
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        indexer_mod._enricher_queue = None
        enricher_mod._enricher_queue = None


@pytest.mark.asyncio
async def test_librarian_leaves_queue_unwired_when_enricher_disabled(
    config, monkeypatch
):
    config.enricher.enabled = False
    monkeypatch.setattr(indexer_mod, "start_indexer", lambda c, db, scan_control=None: None)
    monkeypatch.setattr(indexer_mod, "start_file_watcher", lambda c: None)

    def _fail(*a, **k):  # enricher must not be started when disabled
        raise AssertionError("enricher started while disabled")

    monkeypatch.setattr(enricher_mod, "start_enricher", _fail)

    task = asyncio.create_task(librarian.async_main(config, None))
    try:
        await asyncio.sleep(0.05)
        # No drain side, so the indexer's enrich trigger must be a no-op.
        assert indexer_mod._enricher_queue is None
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        indexer_mod._enricher_queue = None
