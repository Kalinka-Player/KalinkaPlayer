"""TimeLimitedInputModule enforces the SDK latency contract server-side."""

import asyncio
import runpy
import sys
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


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="__protocol_attrs__ is new in Python 3.12"
)
def test_protocol_methods_are_the_members_isinstance_checks():
    assert set(_PROTOCOL_METHODS) == set(InputModule.__protocol_attrs__)


def test_module_loads_on_a_python_without_protocol_attrs(monkeypatch):
    # 3.10 and 3.11 have no __protocol_attrs__, not even on Protocol.
    monkeypatch.delattr(InputModule, "__protocol_attrs__", raising=False)
    monkeypatch.delattr(typing.Protocol, "__protocol_attrs__", raising=False)
    namespace = runpy.run_path(module_timeout.__file__)
    assert namespace["_PROTOCOL_METHODS"] == _PROTOCOL_METHODS


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
