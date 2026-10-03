"""TimeLimitedInputModule enforces the SDK latency contract server-side."""

import asyncio
import runpy
import typing

import pytest

from kalinka_plugin_sdk.datamodel import BrowseItemList
from kalinka_plugin_sdk.inputmodule import InputModule

from kalinka_server import module_timeout
from kalinka_server.module_timeout import _PROTOCOL_METHODS, TimeLimitedInputModule


class SlowModule(InputModule):
    def module_name(self) -> str:
        return "slowpoke"

    async def search(self, type, query, offset=0, limit=50) -> BrowseItemList:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        return BrowseItemList(offset=offset, limit=limit, total=0, items=[])

    # Not part of the InputModule protocol — the server calls this via
    # hasattr, so it reaches the proxy through __getattr__, not __init__.
    async def get_indexer_status(self):
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


def _proxy(timeout_s: float) -> TimeLimitedInputModule:
    return TimeLimitedInputModule(SlowModule(), "slowpoke", timeout_s=timeout_s)


async def test_overrunning_call_raises_timeout_with_context():
    proxy = _proxy(0.05)
    with pytest.raises(TimeoutError, match="slowpoke.search exceeded"):
        await proxy.search(None, "q")


async def test_fast_call_passes_through():
    proxy = _proxy(0.05)
    result = await proxy.browse(None)
    assert isinstance(result, BrowseItemList)


async def test_non_protocol_async_method_is_also_budgeted():
    # get_indexer_status isn't a protocol method, so it comes through
    # __getattr__ — it must still be subject to the per-call budget.
    proxy = _proxy(0.05)
    with pytest.raises(TimeoutError, match="slowpoke.get_indexer_status exceeded"):
        await proxy.get_indexer_status()


async def test_sync_attributes_and_protocol_check_pass_through():
    proxy = _proxy(0.05)
    assert proxy.module_name() == "slowpoke"
    # The server gates modules with isinstance against the runtime-checkable
    # protocol; the proxy must remain indistinguishable there.
    assert isinstance(proxy, InputModule)


def _implementing(names):
    return type("Stub", (), {name: lambda self: None for name in names})()


def test_bound_members_are_exactly_what_the_protocol_check_requires():
    # typing's private protocol attributes differ between versions; isinstance doesn't.
    members = set(_PROTOCOL_METHODS)
    assert isinstance(_implementing(members), InputModule)
    for name in members:
        assert not isinstance(_implementing(members - {name}), InputModule), name


def test_module_loads_on_a_python_without_protocol_attrs(monkeypatch):
    # 3.10 and 3.11 have no __protocol_attrs__, not even on Protocol.
    monkeypatch.delattr(InputModule, "__protocol_attrs__", raising=False)
    monkeypatch.delattr(typing.Protocol, "__protocol_attrs__", raising=False)
    namespace = runpy.run_path(module_timeout.__file__)
    assert namespace["_PROTOCOL_METHODS"] == _PROTOCOL_METHODS


class SlowSourceModule(SlowModule):
    async def get_track_source(self, track_id):
        await asyncio.sleep(0.2)
        return track_id


async def test_a_track_source_has_the_longer_budget():
    proxy = TimeLimitedInputModule(SlowSourceModule(), "slowpoke", timeout_s=0.05)
    assert await proxy.get_track_source("t1") == "t1"


async def test_a_track_source_is_still_bounded(monkeypatch):
    monkeypatch.setattr(module_timeout, "TRACK_SOURCE_TIMEOUT_S", 0.05)
    proxy = TimeLimitedInputModule(SlowSourceModule(), "slowpoke", timeout_s=3)
    with pytest.raises(TimeoutError, match="slowpoke.get_track_source exceeded"):
        await proxy.get_track_source("t1")


async def test_inherited_default_get_all_is_also_budgeted():
    # get_all is inherited from the SDK protocol default; through the proxy
    # it must still be subject to the same per-call budget.
    proxy = _proxy(0.05)
    from kalinka_plugin_sdk.datamodel import EntityId, EntityType

    eid = EntityId(id="x", type=EntityType.ARTIST, source="slowpoke")
    # Default get_all -> self.get, which SlowModule doesn't define -> the
    # protocol stub returns None; the call must complete, not hang.
    out = await proxy.get_all([eid])
    assert out == []
