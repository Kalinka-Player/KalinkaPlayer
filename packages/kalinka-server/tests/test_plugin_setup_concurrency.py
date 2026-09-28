"""Tests for concurrent plugin setup in
``PreparedModuleCollection._scan_and_setup_plugins_from_entry_points``.

A slow plugin ``setup()`` (network login, web-bundle download, subprocess
spawn) used to serialise every other plugin behind it, delaying the server
from accepting connections. Setup now fans out with ``asyncio.gather``. These
tests pin down that the calls overlap, that results come back in entry-point
order (so the default-module choice stays deterministic), and that one
plugin's failure is isolated.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic import Field

from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import PluginBase, PluginType
from kalinka_plugin_sdk import ModuleHealthState

from kalinka_server.player_setup import PreparedModuleCollection


class _Config(ModuleConfig):
    enabled: bool = Field(default=True)


class _Barrier:
    """Minimal asyncio barrier (``asyncio.Barrier`` is 3.11+; the server's
    ``requires-python`` is 3.10).

    The event loop is single-threaded, so the unguarded counter bump is safe.
    Each waiter records its arrival; the last to arrive releases everyone. If
    plugin setup runs sequentially the first (and only) waiter never sees the
    others arrive and blocks forever — the test's ``wait_for`` turns that
    deadlock into a clean, deterministic failure instead of a timing flake.
    """

    def __init__(self, parties: int):
        self._parties = parties
        self._count = 0
        self._released = asyncio.Event()

    async def wait(self):
        self._count += 1
        if self._count >= self._parties:
            self._released.set()
        await self._released.wait()


def _make_plugin(
    plugin_id: str,
    *,
    delay: float = 0.0,
    fail: bool = False,
    barrier: "_Barrier | None" = None,
):
    """Build a fake input-module plugin class whose setup() optionally sleeps
    (to expose serialisation), waits on a shared barrier, and/or raises."""

    started: list[float] = []

    class _Plugin(PluginBase):
        PLUGIN_ID = plugin_id
        REQUIRES_SDK = "1.0"
        PLUGIN_TYPE = PluginType.INPUT_MODULE
        CONFIG_MODEL = _Config

        async def setup(self, context):
            started.append(time.monotonic())
            if barrier is not None:
                await barrier.wait()
            if delay:
                await asyncio.sleep(delay)
            if fail:
                raise RuntimeError(f"{plugin_id} boom")

        def get_interface(self):
            return None

    _Plugin._started = started  # type: ignore[attr-defined]
    return _Plugin


def _collection(plugins) -> PreparedModuleCollection:
    """A collection whose entry-point scan yields the given plugin classes and
    whose plugin-context builder is stubbed (we only exercise the setup flow)."""
    collection = PreparedModuleCollection()
    collection._scan_entry_points = lambda: iter(  # type: ignore[method-assign]
        [(p.PLUGIN_ID, p) for p in plugins]
    )
    # PreparedPlugin.setup reads plugin_context.config.enabled; the rest of the
    # real context (playqueue/eventbus) is irrelevant to the setup flow here.
    collection._make_plugin_context = (  # type: ignore[method-assign]
        lambda name, cls, config: SimpleNamespace(config=config)
    )
    return collection


@pytest.mark.asyncio
async def test_setups_run_concurrently():
    # Prove overlap with a barrier rather than a wall-clock threshold: every
    # plugin must enter setup() before any is allowed to leave. If setup were
    # serialised the first plugin would block at the barrier forever (the
    # others never start), and wait_for would trip — no timing heuristic, so
    # no CI flake.
    barrier = _Barrier(3)
    plugins = [_make_plugin(f"p{i}", barrier=barrier) for i in range(3)]
    collection = _collection(plugins)

    result = await asyncio.wait_for(
        collection._scan_and_setup_plugins_from_entry_points({}), timeout=5
    )

    assert [name for name, _ in result] == ["p0", "p1", "p2"]
    assert all(p.health_state == ModuleHealthState.READY for _, p in result)
    # Every plugin reached setup() — the barrier could only release if all
    # three were in flight at once.
    assert all(len(p._started) == 1 for p in plugins)


@pytest.mark.asyncio
async def test_order_preserved_regardless_of_completion():
    # The last plugin finishes first, the first finishes last — result order
    # must still follow entry-point order so "first enabled" is deterministic.
    plugins = [
        _make_plugin("slow", delay=0.3),
        _make_plugin("medium", delay=0.15),
        _make_plugin("fast", delay=0.01),
    ]
    collection = _collection(plugins)

    result = await collection._scan_and_setup_plugins_from_entry_points({})

    assert [name for name, _ in result] == ["slow", "medium", "fast"]


@pytest.mark.asyncio
async def test_one_failure_is_isolated():
    plugins = [
        _make_plugin("ok1"),
        _make_plugin("broken", fail=True),
        _make_plugin("ok2"),
    ]
    collection = _collection(plugins)

    result = await collection._scan_and_setup_plugins_from_entry_points({})
    by_name = dict(result)

    assert [name for name, _ in result] == ["ok1", "broken", "ok2"]
    assert by_name["ok1"].health_state == ModuleHealthState.READY
    assert by_name["ok2"].health_state == ModuleHealthState.READY
    assert by_name["broken"].health_state == ModuleHealthState.ERROR
    assert "boom" in (by_name["broken"].error_message or "")


@pytest.mark.asyncio
async def test_disabled_plugin_is_not_set_up():
    plugin = _make_plugin("off")
    collection = _collection([plugin])
    overrides = {"input_modules.off.enabled": False}

    result = await collection._scan_and_setup_plugins_from_entry_points(overrides)
    by_name = dict(result)

    assert by_name["off"].health_state == ModuleHealthState.DISABLED
    assert plugin._started == []  # setup() never called
