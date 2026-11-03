"""Shutting the workers down takes seconds per process; the server, which
may be setting the module up again on new settings, keeps playing meanwhile."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles


class _SlowProcess:
    pid = 4242

    def __init__(self):
        self.terminated = False

    def is_alive(self) -> bool:
        return not self.terminated

    def terminate(self) -> None:
        self.terminated = True

    def join(self, timeout=None) -> None:
        time.sleep(0.3)

    def kill(self) -> None:
        pass


@pytest.mark.asyncio
async def test_shutting_the_workers_down_does_not_block_the_loop():
    plugin = KalinkaPluginLocalFiles()
    plugin._librarian_proc = _SlowProcess()
    plugin._discovery = SimpleNamespace(stop=lambda: None)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticking = asyncio.create_task(tick())
    try:
        await plugin.shutdown()
    finally:
        ticking.cancel()

    assert plugin._librarian_proc.terminated
    assert ticks >= 5
