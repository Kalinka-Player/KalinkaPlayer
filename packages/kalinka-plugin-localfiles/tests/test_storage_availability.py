#!/usr/bin/env python3
"""Deciding whether a music folder is reachable right now.

Every storage answers the same question and gets the same treatment around
it, which is why the bounding, the deduplication and the retry window live on
the interface rather than in each implementation. What they buy:

  * a folder that never answers costs one blocked worker thread however many
    callers are waiting on it, instead of draining the executor;
  * storage that is slow to come up — an automount completing, a NAS spinning
    its disks back up — gets a few seconds rather than one attempt.
"""

import asyncio
import threading

import pytest

from kalinka_plugin_localfiles.storage.base import FileStorage, RootStatus
from kalinka_plugin_localfiles.storage.local import LocalStorage


class _CountingStorage(FileStorage):
    """A storage whose only behaviour is how it answers a probe."""

    def __init__(self, verdicts):
        super().__init__()
        self._verdicts = list(verdicts)
        self.probes = 0
        self.gate = threading.Event()
        self.gate.set()

    @property
    def scheme(self):
        return "counting"

    def handles(self, path):
        return True

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
        self.probes += 1
        self.gate.wait(timeout=5)
        available = self._verdicts.pop(0) if self._verdicts else True
        if not available:
            return self.unavailable(root, "not yet")
        return RootStatus(
            root=root,
            available=True,
            empty=False,
            reason="",
            fs_type="counting",
            is_network=True,
            is_autofs=False,
            identity="counting 1",
        )


@pytest.mark.asyncio
async def test_piled_up_callers_share_one_probe():
    """A hung folder may cost one worker thread, never one per request."""
    storage = _CountingStorage([True])
    storage.gate.clear()

    first = asyncio.create_task(storage.probe_root("/root", timeout=5))
    second = asyncio.create_task(storage.probe_root("/root", timeout=5))
    await asyncio.sleep(0.05)
    storage.gate.set()
    statuses = await asyncio.gather(first, second)

    assert storage.probes == 1
    assert all(status.available for status in statuses)


@pytest.mark.asyncio
async def test_a_probe_that_never_answers_is_bounded():
    storage = _CountingStorage([True])
    storage.gate.clear()
    try:
        status = await storage.probe_root("/root", timeout=0.05)
        assert not status.available
        assert "did not respond" in status.reason
    finally:
        storage.gate.set()


@pytest.mark.asyncio
async def test_storage_that_comes_up_late_is_waited_for():
    storage = _CountingStorage([False, False, True])

    status = await storage.await_root_available("/root", deadline_s=5.0)

    assert status.available
    assert storage.probes >= 3


@pytest.mark.asyncio
async def test_the_wait_gives_up_at_the_deadline():
    storage = _CountingStorage([False] * 20)

    status = await storage.await_root_available("/root", deadline_s=0.2)

    assert not status.available


@pytest.mark.asyncio
async def test_nothing_defers_a_probe_by_default():
    """Only local storage has a reason to hold back — the stat inside a probe
    is what re-triggers an automount."""
    assert not _CountingStorage([]).should_defer_probe("/root")


@pytest.mark.asyncio
async def test_a_local_folder_that_appears_late(tmp_path):
    root = tmp_path / "late"
    storage = LocalStorage()
    real_probe = storage.probe_root_blocking
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] >= 2:
            root.mkdir(exist_ok=True)
            (root / "a.flac").write_bytes(b"x")
        return real_probe(path)

    storage.probe_root_blocking = flaky
    status = await storage.await_root_available(str(root), deadline_s=5.0)

    assert status.available
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_a_local_folder_that_never_appears(tmp_path):
    status = await LocalStorage().await_root_available(
        str(tmp_path / "never"), deadline_s=0.2
    )
    assert not status.available
