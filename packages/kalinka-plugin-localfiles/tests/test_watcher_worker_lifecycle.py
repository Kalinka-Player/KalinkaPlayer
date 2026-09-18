#!/usr/bin/env python3
"""What happens to the other watchers when one of them gives up.

One worker drives a watch loop per storage, and the loops are independent:
inotify failing on the local folders says nothing about the share, which
cannot report changes through it anyway. The failure to avoid is the quiet
one — a loop that dies taking nobody with it, leaving its siblings polling
descriptors that nothing will ever close.
"""

import asyncio
import time

import pytest

import kalinka_plugin_localfiles.indexer.indexer as indexer_mod
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.storage import (
    ChangeWatcher,
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
            empty=False,
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
