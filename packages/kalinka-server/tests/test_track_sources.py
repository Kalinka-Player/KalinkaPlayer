"""A queued track's source, asked of the module that owns it when it plays."""

from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from kalinka_plugin_sdk import ModuleHealthState
from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.inputmodule import (
    DirectUrl,
    InputModule,
    SourceUnavailableError,
    TrackSource,
)
from kalinka_server.player_setup import PreparedModuleCollection, PreparedPlugin
from kalinka_server.track_sources import ModuleTrackSources


def _track_id(source: str, local: str) -> EntityId:
    return EntityId(id=local, type=EntityType.TRACK, source=source)


class _Module(InputModule):
    def __init__(self, name: str):
        self.name = name
        self.asked: list[str] = []

    def module_name(self) -> str:
        return self.name

    async def get_track_source(self, track_id):
        self.asked.append(track_id)
        return TrackSource(
            source=DirectUrl(url=f"https://{self.name}.test/{track_id}"), format="flac"
        )


class _Registry:
    def __init__(self, modules: dict[str, InputModule]):
        self.modules = modules

    def enabled_input_module(self, name: str) -> Optional[InputModule]:
        return self.modules.get(name)


async def test_a_track_is_asked_of_the_module_that_owns_it():
    qobuz, library = _Module("qobuz"), _Module("localfiles")
    sources = ModuleTrackSources(_Registry({"qobuz": qobuz, "localfiles": library}))

    source = await sources.resolve(_track_id("qobuz", "q1"))

    assert source.source.url == "https://qobuz.test/q1"
    assert qobuz.asked == ["q1"]
    assert library.asked == []


async def test_a_track_whose_module_is_not_enabled_is_unavailable_for_now():
    sources = ModuleTrackSources(_Registry({}))

    with pytest.raises(SourceUnavailableError, match="source is not available"):
        await sources.resolve(_track_id("qobuz", "q1"))


async def test_a_module_switched_on_after_the_track_was_queued_serves_it():
    registry = _Registry({})
    sources = ModuleTrackSources(registry)
    with pytest.raises(SourceUnavailableError):
        await sources.resolve(_track_id("qobuz", "q1"))

    registry.modules["qobuz"] = _Module("qobuz")

    source = await sources.resolve(_track_id("qobuz", "q1"))
    assert source.source.url == "https://qobuz.test/q1"


async def test_the_modules_reason_reaches_the_queue():
    library = Mock()
    library.get_track_source = AsyncMock(
        side_effect=SourceUnavailableError("Music folder /mnt/nas is not available")
    )
    sources = ModuleTrackSources(_Registry({"localfiles": library}))

    with pytest.raises(SourceUnavailableError, match="^Music folder /mnt/nas"):
        await sources.resolve(_track_id("localfiles", "t1"))


def test_only_a_track_with_no_enabled_module_is_known_unavailable_unasked():
    qobuz = Mock()
    registry = _Registry({"qobuz": qobuz})
    sources = ModuleTrackSources(registry)

    assert sources.unavailable_reason(_track_id("qobuz", "q1")) is None
    assert (
        sources.unavailable_reason(_track_id("upnp", "u1"))
        == "This track's source is not available"
    )
    qobuz.assert_not_called()
    assert qobuz.mock_calls == []

    registry.modules["upnp"] = _Module("upnp")
    assert sources.unavailable_reason(_track_id("upnp", "u1")) is None


def _prepared(interface, health=ModuleHealthState.READY) -> PreparedPlugin:
    return PreparedPlugin(
        plugin_class=Mock(),
        plugin_instance=None,
        health_state=health,
        plugin_context=Mock(),
        interface=interface,
    )


def test_the_module_collection_answers_for_enabled_input_modules_only():
    module = _Module("on")
    collection = PreparedModuleCollection(
        prepared_input_modules={
            "on": _prepared(module),
            "off": _prepared(None, ModuleHealthState.DISABLED),
            "torn_down": _prepared(None),
            "not_input": _prepared(object()),
        },
        enabled_input_modules={"on", "torn_down", "not_input"},
    )

    assert collection.enabled_input_module("on") is module
    assert collection.enabled_input_module("off") is None
    assert collection.enabled_input_module("torn_down") is None
    assert collection.enabled_input_module("not_input") is None
    assert collection.enabled_input_module("never_installed") is None
