#!/usr/bin/env python3
"""What happens to the other watchers when one of them gives up.

One worker drives a watch loop per storage, and the loops are independent:
inotify failing on the local folders says nothing about the share, which
cannot report changes through it anyway. The failure to avoid is the quiet
one — a loop that dies taking nobody with it, leaving its siblings polling
descriptors that nothing will ever close.
"""

import asyncio
import logging
import queue as thread_queue
import time

import pytest

import kalinka_plugin_localfiles.indexer.indexer as indexer_mod
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.storage import (
    ChangeWatcher,
    ChangeKind,
    FileStorage,
    RootStatus,
    StorageResolver,
    WatchResult,
)


class _Watcher(ChangeWatcher):
    def __init__(self, arming_error=None):
        self.arming_error = arming_error
        self.closes = 0
        self.polls = 0
        self.armed = []

    def watch(self, root):
        if self.arming_error is not None:
            raise self.arming_error
        self.armed.append(root)
        return True

    def unwatch(self, root):
        self.armed.remove(root)

    def poll(self, timeout_s):
        self.polls += 1
        return WatchResult()

    def close(self):
        self.closes += 1


class _Storage(FileStorage):
    """A storage that exists to hand out one watcher."""

    def __init__(self, scheme, watcher):
        super().__init__()
        self._scheme = scheme
        self._watcher = watcher

    @property
    def scheme(self):
        return self._scheme

    def watcher(self):
        return self._watcher

    def handles(self, path):
        return path.startswith(f"{self._scheme}://")

    def canonical(self, path):
        return path

    def contains(self, path, roots):
        return any(path.startswith(root) for root in roots)

    def listdir(self, path):
        raise OSError("not a real storage")

    def stat(self, path):
        raise OSError("not a real storage")

    def open(self, path):
        raise OSError("not a real storage")

    def probe_root_blocking(self, root):
        return RootStatus(
            root=root,
            available=True,
            reason="",
            fs_type=self._scheme,
            is_network=False,
            is_autofs=False,
            identity=f"{self._scheme} 1",
        )


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """Runs the worker over two storages, the first of which may be broken."""

    async def run(first, second):
        config = LocalFilesConfig(
            music_folders=["alpha://root", "beta://root"],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
        storages = [_Storage("alpha", first), _Storage("beta", second)]
        monkeypatch.setattr(
            indexer_mod, "build_resolver", lambda _config: StorageResolver(storages)
        )
        monkeypatch.setattr(indexer_mod, "WATCH_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(indexer_mod, "_indexer_queue", asyncio.Queue())
        indexer_mod._file_watcher_stop_event = asyncio.Event()
        task = asyncio.create_task(indexer_mod._file_watcher_worker(config))
        await _until(lambda: task.done() or second.polls >= 3)
        return task

    return run


async def _until(condition, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not condition():
        await asyncio.sleep(0.02)


async def _stop(task):
    indexer_mod._file_watcher_stop_event.set()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_loop_that_cannot_arm_leaves_its_siblings_watching(worker, caplog):
    broken = _Watcher(arming_error=RuntimeError("no watch descriptors left"))
    healthy = _Watcher()

    task = await worker(broken, healthy)

    assert broken.closes >= 1
    assert healthy.polls >= 3
    assert not task.done()
    assert any("alpha" in record.getMessage() for record in caplog.records)

    await _stop(task)
    assert healthy.closes >= 1


@pytest.mark.asyncio
async def test_a_watcher_armed_and_then_abandoned_is_closed(worker):
    """Arming happens before the loop, so a failure there used to escape the
    block that closes the watcher — and inotify descriptors are a finite
    per-user resource."""
    half_armed = _Watcher()
    original = half_armed.watch

    def arm_then_fail(root):
        original(root)
        raise RuntimeError("the second call is the one that fails")

    half_armed.watch = arm_then_fail
    healthy = _Watcher()

    task = await worker(half_armed, healthy)

    assert half_armed.closes >= 1
    await _stop(task)


@pytest.mark.asyncio
async def test_every_watcher_is_closed_when_the_worker_stops(worker):
    first, second = _Watcher(), _Watcher()

    task = await worker(first, second)
    await _stop(task)

    assert (first.closes, second.closes) >= (1, 1)


@pytest.mark.asyncio
async def test_a_storage_that_cannot_report_changes_is_left_to_the_scan(
    tmp_path, monkeypatch
):
    config = LocalFilesConfig(
        music_folders=["alpha://root"],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    storage = _Storage("alpha", None)
    monkeypatch.setattr(
        indexer_mod, "build_resolver", lambda _config: StorageResolver([storage])
    )
    indexer_mod._file_watcher_stop_event = asyncio.Event()

    await asyncio.wait_for(indexer_mod._file_watcher_worker(config), timeout=5)


@pytest.mark.asyncio
async def test_cancelling_the_worker_closes_the_watchers(worker):
    first, second = _Watcher(), _Watcher()

    task = await worker(first, second)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (first.closes, second.closes) >= (1, 1)


@pytest.mark.asyncio
async def test_events_accumulate_in_one_batch_while_the_indexer_is_busy(tmp_path, monkeypatch, caplog):
    from kalinka_plugin_localfiles.storage import ChangeKind

    class BusyWatcher(_Watcher):
        def poll(self, timeout_s):
            time.sleep(.005)
            self.polls += 1
            return WatchResult(changes={
                (ChangeKind.FILE_WRITTEN, f"alpha://root/{self.polls}.flac")
            })

    watcher = BusyWatcher()
    storage = _Storage("alpha", watcher)
    config = LocalFilesConfig(
        db_path=str(tmp_path / "db.sqlite"), artwork_path=str(tmp_path / "art")
    )
    queue = asyncio.Queue()
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(indexer_mod, "_indexer_queue", queue)
    monkeypatch.setattr(indexer_mod, "_file_watcher_stop_event", asyncio.Event())
    task = asyncio.create_task(indexer_mod._watch_roots(
        config, storage, watcher, ["alpha://root"]
    ))
    try:
        await _until(lambda: watcher.polls >= 10)
        assert queue.qsize() == 1
        assert not any(
            "File watcher detected" in record.message or "Changes added to indexer queue" in record.message
            for record in caplog.records
        )
        command = await queue.get()
        changes = command.take()
        assert len(changes) >= 9
        queue.task_done()
        count = len(changes)
        await _until(lambda: queue.qsize() == 1)
        assert len(changes) == count  # an in-progress batch is no longer mutated
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_unsupported_notifications_are_left_to_scans_without_rearming(tmp_path, monkeypatch):
    class UnsupportedWatcher(_Watcher):
        def poll(self, timeout_s):
            time.sleep(.005)
            self.polls += 1
            return WatchResult(unsupported={"alpha://root"}) if self.polls == 1 else WatchResult()

    watcher = UnsupportedWatcher()
    storage = _Storage("alpha", watcher)
    config = LocalFilesConfig(
        db_path=str(tmp_path / "db.sqlite"), artwork_path=str(tmp_path / "art")
    )
    monkeypatch.setattr(indexer_mod, "REARM_CHECK_INTERVAL_S", 0)
    monkeypatch.setattr(indexer_mod, "_file_watcher_stop_event", asyncio.Event())
    task = asyncio.create_task(indexer_mod._watch_roots(
        config, storage, watcher, ["alpha://root"]
    ))
    try:
        await _until(lambda: watcher.polls >= 10)
        assert watcher.armed == ["alpha://root"]
    finally:
        await _stop(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_indexer", [False, True])
async def test_startup_reconnect_and_overflow_share_one_reconciliation_queue(
    tmp_path, monkeypatch, busy_indexer
):
    root = "alpha://root"

    class EventWatcher(_Watcher):
        events = thread_queue.Queue()

        def poll(self, timeout_s):
            self.polls += 1
            try:
                return self.events.get(timeout=timeout_s)
            except thread_queue.Empty:
                return WatchResult()

    class Db:
        async def get_root_signature(self, root):
            return "alpha 1"  # Same identity still owes an SMB reconnect scan.

    watcher = EventWatcher()
    queue = asyncio.Queue()
    monkeypatch.setattr(indexer_mod, "AsyncIndexerDb", lambda config: Db())
    monkeypatch.setattr(indexer_mod, "_indexer_queue", queue)
    monkeypatch.setattr(indexer_mod, "_file_watcher_stop_event", asyncio.Event())
    monkeypatch.setattr(indexer_mod, "WATCH_POLL_INTERVAL_S", .01)
    monkeypatch.setattr(indexer_mod, "REARM_CHECK_INTERVAL_S", 0)
    config = LocalFilesConfig(
        db_path=str(tmp_path / "db.sqlite"), artwork_path=str(tmp_path / "art")
    )
    task = asyncio.create_task(indexer_mod._watch_roots(
        config, _Storage("alpha", watcher), watcher, [root]
    ))

    async def take_scan():
        command = await asyncio.wait_for(queue.get(), 2)
        assert isinstance(command, indexer_mod._ChangeBatch)
        assert command.take() == {(ChangeKind.DIR_ADDED, root)}
        queue.task_done()

    try:
        await _until(lambda: queue.qsize() == 1)
        assert watcher.armed == [root]
        if not busy_indexer:
            await take_scan()
        for count in range(2, 5):
            watcher.events.put(WatchResult(vanished={root}))
            await _until(lambda: len(watcher.armed) == count)
            assert len(watcher.armed) == count
            if not busy_indexer:
                await take_scan()
        watcher.events.put(WatchResult(rescan={root}))
        if not busy_indexer:
            await take_scan()
        # Let later poll cycles expose a duplicate scan after the consumer
        # has claimed each batch, or an extra command behind a busy consumer.
        observed = watcher.polls
        await _until(lambda: watcher.polls >= observed + 3)
        assert queue.qsize() == (1 if busy_indexer else 0)
        if busy_indexer:
            await take_scan()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_rearm_failure_keeps_retryable_root_and_other_roots_reconciliation():
    class Db:
        async def get_root_signature(self, root):
            if root.endswith("/b"):
                raise OSError("database temporarily unavailable")
            return "alpha 1"

    watcher = _Watcher()
    lost = {"alpha://root/a": False, "alpha://root/b": False}
    rescan = await indexer_mod._rearm_lost_roots(
        _Storage("alpha", watcher), watcher, lost, Db()
    )
    assert rescan == {"alpha://root/a"}
    assert lost == {"alpha://root/b": False}
