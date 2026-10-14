"""Manual checks use the existing worker, timing and file safety rules."""

import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer import indexer as indexer_mod
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.scan_control import ScanControl


@pytest_asyncio.fixture
async def library(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "art"),
        quiescence_seconds=0,
        enricher={"enabled": False},
        ai_search={"enabled": False},
    )
    await init_db(config.db_path)
    db = AsyncIndexerDb(config)
    return config, db, music


@pytest.mark.asyncio
async def test_no_change_check_is_persisted_and_survives_a_failed_check(
    library, monkeypatch
):
    config, db, _ = library
    fi = FileIndexer(config, db)
    control = ScanControl()
    control.available.set()
    control.scanning.set()
    module = LocalFilesInputModule(
        config, LocalFilesInputModuleDb(config), scan_control=control
    )
    await fi.run_scan()
    first = (await db.get_scan_progress())["last_scan_at"]
    assert first is not None
    status = (await module.get_indexer_status())["indexing"]
    assert status["scanning"] and status["last_scan_at"] == first
    assert status["pending"] == 0 and status["total"] == 0

    async def broken_walk(*_):
        assert (await db.get_scan_progress())["active"]
        raise OSError("Storage unavailable")

    fresh = FileIndexer(config, AsyncIndexerDb(config))
    monkeypatch.setattr(fresh, "_audio_files_by_folder", broken_walk)
    with pytest.raises(OSError):
        await fresh.run_scan()
    progress = await db.get_scan_progress()
    assert progress["last_scan_at"] == first
    assert progress["active"] is False


@pytest.mark.asyncio
async def test_scan_stays_active_through_cleanup(library, monkeypatch):
    config, db, _ = library
    fi = FileIndexer(config, db)
    cleanup = fi.cleanup_stale_tracks

    async def checked_cleanup():
        progress = await db.get_scan_progress()
        assert progress["active"]
        assert progress["last_scan_at"] is None
        return await cleanup()

    monkeypatch.setattr(fi, "cleanup_stale_tracks", checked_cleanup)
    await fi.run_scan()
    assert (await db.get_scan_progress())["last_scan_at"] is not None


@pytest.mark.asyncio
async def test_status_reports_pending_but_never_claims_a_dead_worker_is_scanning(
    library,
):
    config, db, _ = library
    control = ScanControl()
    alive = True
    module = LocalFilesInputModule(
        config,
        LocalFilesInputModuleDb(config),
        scan_control=control,
        librarian_alive=lambda: alive,
    )
    with pytest.raises(RuntimeError):
        await module.request_rescan()
    control.available.set()
    await module.request_rescan()
    await module.request_rescan()
    assert (await module.get_indexer_status())["indexing"]["scan_pending"]
    control.scanning.set()
    await db.set_scan_progress(20, 5, True, changed=2, last_scan_at=123.5)
    status = (await module.get_indexer_status())["indexing"]
    assert status["pending"] == 15 and status["last_scan_at"] == 123.5
    alive = False
    with pytest.raises(RuntimeError):
        await module.request_rescan()
    status = (await module.get_indexer_status())["indexing"]
    assert (
        not status["available"]
        and not status["scanning"]
        and not status["scan_pending"]
    )
    assert status["total"] == 0 and status["last_scan_at"] == 123.5


@pytest.mark.asyncio
async def test_empty_library_has_a_reachable_folder_catalog(library):
    config, _, _ = library
    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    listing = module._browse_root(0, 10)
    assert listing.total == 1
    assert listing.items[0].id.id == "files"
    assert not module._browse_root(1, 10).items


@pytest.mark.asyncio
@pytest.mark.parametrize("extraction_failed", [False, True])
@pytest.mark.parametrize("change", ["grow", "retag", "replace", "delete"])
async def test_file_changing_while_read_is_deferred_without_a_failure_record(
    library,
    monkeypatch,
    change,
    extraction_failed,
):
    config, db, music = library
    path = music / "arriving.mp3"
    path.write_bytes(b"partial")
    fi = FileIndexer(config, db)

    def extract(*_):
        if change == "grow":
            path.write_bytes(b"partial but still growing")
        elif change == "retag":
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        elif change == "replace":
            before = path.stat()
            other = music / "replacement"
            other.write_bytes(b"partial")
            os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
            other.replace(path)
        else:
            path.unlink()
        return (
            None
            if extraction_failed
            else {"title": "Incomplete", "format": "audio/mpeg"}
        )

    monkeypatch.setattr(fi, "_extract_metadata", extract)
    assert await fi.process_file(str(path)) is None
    assert await db.get_track_by_path(str(path)) is None
    assert await db.get_failure(str(path)) is None
    if change != "delete":
        assert str(path) in fi._deferred_files


@pytest.mark.asyncio
async def test_recent_file_that_keeps_growing_waits_for_a_later_check(
    library, monkeypatch
):
    config, db, music = library
    config.quiescence_seconds = 5
    path = music / "arriving.mp3"
    path.write_bytes(b"partial")
    now = path.stat().st_mtime + 1
    monkeypatch.setattr(
        indexer_mod, "time", SimpleNamespace(time=lambda: now, monotonic=lambda: 0)
    )
    fi = FileIndexer(config, db)

    async def still_writing(delay):
        assert 0 < delay <= 5
        path.write_bytes(b"partial and growing")

    monkeypatch.setattr(indexer_mod.asyncio, "sleep", still_writing)
    monkeypatch.setattr(
        fi, "_extract_metadata", lambda *_: pytest.fail("Read a growing file")
    )
    assert await fi.process_file(str(path)) is None
    assert await db.get_failure(str(path)) is None


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class _IdleRetries:
    def deferred_retry_in(self):
        return 30

    async def retry_deferred_files(self):
        pass


@pytest.mark.asyncio
async def test_deferred_files_are_retried_without_another_watcher_event(
    library, monkeypatch
):
    config, db, _ = library
    scans = []
    retried = asyncio.Event()

    class Indexer:
        def __init__(self, *_):
            pass

        async def start(self):
            scans.append(1)

        def deferred_retry_in(self):
            return 0.01

        async def retry_deferred_files(self):
            retried.set()
            await indexer_mod._indexer_queue.put("stop")

    monkeypatch.setattr(indexer_mod, "FileIndexer", Indexer)
    monkeypatch.setattr(indexer_mod, "_indexer_queue", asyncio.Queue())
    monkeypatch.setattr(indexer_mod, "_shutdown_event", asyncio.Event())
    async with asyncio.timeout(3):
        await indexer_mod._indexer_worker(config, db, ScanControl())
    assert retried.is_set() and len(scans) == 1


@pytest.mark.asyncio
async def test_repeated_requests_queue_one_pass_behind_current_scan(
    library, monkeypatch
):
    config, db, _ = library
    control = ScanControl()
    gate = asyncio.Event()
    scans = []
    active = 0

    class Indexer(_IdleRetries):
        def __init__(self, *_):
            pass

        async def start(self):
            nonlocal active
            active += 1
            assert active == 1
            scans.append(len(scans))
            await gate.wait()
            active -= 1

    monkeypatch.setattr(indexer_mod, "FileIndexer", Indexer)
    monkeypatch.setattr(indexer_mod, "_indexer_queue", asyncio.Queue())
    monkeypatch.setattr(indexer_mod, "_shutdown_event", asyncio.Event())
    worker = asyncio.create_task(indexer_mod._indexer_worker(config, db, control))
    try:
        await _until(lambda: len(scans) == 1)
        for _ in range(20):
            control.request()
        assert control.pending.is_set()
        gate.set()
        await _until(lambda: len(scans) == 2 and not control.scanning.is_set())
        await indexer_mod._indexer_queue.put("stop")
        await worker
        assert len(scans) == 2
        assert not control.available.is_set() and not control.pending.is_set()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_watcher_events_do_not_postpone_periodic_checks(library, monkeypatch):
    config, db, _ = library
    control = ScanControl()
    clock = 0.0
    scans = []
    increments = []

    class Indexer(_IdleRetries):
        def __init__(self, *_):
            pass

        async def start(self):
            scans.append(clock)

        async def handle_incremental_changes(self, changes):
            nonlocal clock
            clock += 300
            increments.append(changes)

    monkeypatch.setattr(indexer_mod, "FileIndexer", Indexer)
    monkeypatch.setattr(indexer_mod, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(indexer_mod, "_indexer_queue", asyncio.Queue())
    monkeypatch.setattr(indexer_mod, "_shutdown_event", asyncio.Event())
    config.scan_interval_minutes = 15
    worker = asyncio.create_task(indexer_mod._indexer_worker(config, db, control))
    try:
        await _until(lambda: len(scans) == 1)
        for i in range(3):
            await indexer_mod._indexer_queue.put({"incremental_changes": i})
        await _until(lambda: len(scans) == 2)
        assert scans == [0, 900]
        assert increments == [0, 1, 2]
        await indexer_mod._indexer_queue.put("stop")
        await worker
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def _run_librarian(config, control):
    from kalinka_plugin_localfiles.librarian import async_main

    asyncio.run(async_main(config, scan_control=control))


@pytest.mark.asyncio
async def test_request_reaches_the_existing_librarian_process(library):
    import multiprocessing

    config, db, _ = library
    control = ScanControl()
    process = multiprocessing.Process(target=_run_librarian, args=(config, control))
    process.start()
    module = LocalFilesInputModule(
        config,
        LocalFilesInputModuleDb(config),
        scan_control=control,
        librarian_alive=process.is_alive,
    )
    try:
        async with asyncio.timeout(15):
            first = None
            while first is None:
                progress = await db.get_scan_progress()
                first = (progress or {}).get("last_scan_at")
                await asyncio.sleep(0.02)
            await module.request_rescan()
            while True:
                status = (await module.get_indexer_status())["indexing"]
                if status["last_scan_at"] > first and not status["scanning"]:
                    break
                await asyncio.sleep(0.02)
        assert process.is_alive()
        assert status["available"] and not status["scan_pending"]
    finally:
        process.terminate()
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 5)
        process.close()
