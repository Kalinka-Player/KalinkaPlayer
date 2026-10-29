"""A module is asked for data only while it says it can serve.

Setup succeeding is not enough: Jamendo without a client id sets up fine and
then reports an error, and its catalogs were still requested — empty. What a
plugin reports of itself decides whether requests reach it, and that follows
it while the server runs.
"""

import asyncio
import gc
from types import SimpleNamespace

import pytest
from pydantic import Field

from kalinka_plugin_sdk import ModuleHealthState, ModuleState
from kalinka_plugin_sdk import __version__ as SDK_VERSION
from kalinka_plugin_sdk.datamodel import EntityId
from kalinka_plugin_sdk.inputmodule import InputModule, SourceUnavailableError
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import InputModulePlugin, PluginBase

from kalinka_server import player_setup, server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.player_setup import (
    PreparedModuleCollection,
    PreparedPlugin,
    modules,
)
from tests.app_harness import isolate_app, running

NO_KEY = "No client id configured."


class _Module(InputModule):
    def __init__(self, name: str):
        self._name = name

    def module_name(self) -> str:
        return self._name

    async def get_content_info(self, asset_id: str):
        raise SourceUnavailableError("the share is waking up")


class _Plugin:
    """A plugin whose health is whatever the test says it is."""

    def __init__(self, state=ModuleHealthState.READY, message=None):
        self.health = ModuleState(state=state, message=message)
        self.asked = 0

    async def get_state(self) -> ModuleState:
        self.asked += 1
        return self.health


class _RaisingPlugin:
    async def get_state(self) -> ModuleState:
        raise RuntimeError("401 for https://api.test/?client_id=s3cret")


class _MistypedPlugin:
    async def get_state(self):
        return {"state": "error"}


class _SilentPlugin:
    async def get_state(self):
        await asyncio.Event().wait()


class _CancelledPlugin:
    async def get_state(self) -> ModuleState:
        raise asyncio.CancelledError


class _HeldPlugin:
    """Answers only once the test lets it — with a failure, if asked to."""

    def __init__(self, failure: Exception | None = None):
        self.asked = 0
        self.release = asyncio.Event()
        self._failure = failure

    async def get_state(self) -> ModuleState:
        self.asked += 1
        await self.release.wait()
        if self._failure is not None:
            raise self._failure
        return ModuleState(state=ModuleHealthState.READY)


def _prepared(name, plugin, health=ModuleHealthState.READY, error=None):
    return PreparedPlugin(
        plugin_class=PluginBase,
        plugin_instance=plugin,
        health_state=health,
        plugin_context=SimpleNamespace(config=SimpleNamespace(name=name)),
        interface=_Module(name),
        error_message=error,
    )


def _collection(**plugins) -> PreparedModuleCollection:
    return PreparedModuleCollection(
        prepared_input_modules={
            name: _prepared(name, plugin) for name, plugin in plugins.items()
        }
    )


async def test_a_module_that_reports_an_error_is_not_reached():
    jamendo = _Plugin(ModuleHealthState.ERROR, NO_KEY)
    collection = _collection(jamendo=jamendo, localfiles=_Plugin())

    await collection.refresh_health()

    assert collection.enabled_input_modules == {"localfiles"}
    assert collection.enabled_input_module("jamendo") is None
    assert collection.prepared_input_modules["jamendo"].state.message == NO_KEY


async def test_a_module_that_reports_an_error_still_plays():
    collection = _collection(qobuz=_Plugin(ModuleHealthState.ERROR))
    collection.prepared_input_modules["broken"] = _prepared(
        "broken", _Plugin(), ModuleHealthState.ERROR, "setup failed"
    )

    await collection.refresh_health()

    assert collection.enabled_input_module("qobuz") is None
    assert collection.set_up_input_module("qobuz") is not None
    assert collection.set_up_input_module("broken") is None


async def test_a_module_that_recovers_is_reached_again():
    qobuz = _Plugin(ModuleHealthState.ERROR)
    collection = _collection(qobuz=qobuz)
    await collection.refresh_health()
    assert collection.enabled_input_module("qobuz") is None

    qobuz.health = ModuleState(state=ModuleHealthState.READY)
    await collection.refresh_health()

    assert collection.enabled_input_module("qobuz") is not None


@pytest.mark.parametrize(
    "state, served",
    [
        (ModuleHealthState.READY, True),
        (ModuleHealthState.WARNING, True),
        (ModuleHealthState.ERROR, False),
        (ModuleHealthState.DISABLED, False),
    ],
)
async def test_only_a_module_that_says_it_can_answer_is_reached(state, served):
    collection = _collection(source=_Plugin(state))

    await collection.refresh_health()

    assert ("source" in collection.enabled_input_modules) is served


async def test_a_plugin_that_failed_setup_is_not_asked():
    plugin = _Plugin()
    collection = PreparedModuleCollection(
        prepared_input_modules={
            "broken": _prepared(
                "broken", plugin, ModuleHealthState.ERROR, "setup failed"
            )
        }
    )

    await collection.refresh_health()

    assert plugin.asked == 0
    assert collection.enabled_input_modules == set()
    state = collection.prepared_input_modules["broken"].state
    assert (state.state, state.message) == (ModuleHealthState.ERROR, "setup failed")


@pytest.mark.parametrize(
    "plugin, failure",
    [(_RaisingPlugin(), "RuntimeError"), (_MistypedPlugin(), "TypeError")],
    ids=["raises", "mistyped"],
)
async def test_a_plugin_whose_answer_fails_is_a_warning(plugin, failure, caplog):
    collection = _collection(source=_Plugin(ModuleHealthState.ERROR))
    await collection.refresh_health()
    prepared = collection.prepared_input_modules["source"]
    prepared.plugin_instance = plugin

    await collection.refresh_health()

    assert prepared.state.state == ModuleHealthState.WARNING
    assert failure in prepared.state.message
    assert "s3cret" not in prepared.state.message
    assert "source" in collection.enabled_input_modules
    assert not prepared.reports_ready
    assert failure in caplog.text
    assert "s3cret" not in caplog.text


async def test_a_plugin_whose_answer_is_cancelled_is_a_warning():
    collection = _collection(source=_CancelledPlugin())

    await collection.refresh_health()

    state = collection.prepared_input_modules["source"].state
    assert state.state == ModuleHealthState.WARNING
    assert "CancelledError" in state.message
    assert "source" in collection.enabled_input_modules


@pytest.mark.parametrize(
    "state, ready",
    [
        (ModuleHealthState.READY, True),
        (ModuleHealthState.WARNING, False),
        (ModuleHealthState.ERROR, False),
        (ModuleHealthState.DISABLED, False),
    ],
)
async def test_only_a_plugin_that_says_ready_is_given_the_output(state, ready):
    prepared = _prepared("amp", _Plugin(state))

    await prepared.refresh_state()

    assert prepared.reports_ready is ready


def test_a_plugin_not_yet_asked_is_ready_on_setups_word():
    assert _prepared("amp", _Plugin()).reports_ready
    assert not _prepared(
        "amp", _Plugin(), ModuleHealthState.ERROR, "setup failed"
    ).reports_ready


async def test_a_plugin_that_does_not_answer_does_not_hold_up_the_rest(
    monkeypatch,
):
    monkeypatch.setattr(player_setup, "PLUGIN_CALL_TIMEOUT_S", 0.01)
    collection = _collection(
        silent=_SilentPlugin(), jamendo=_Plugin(ModuleHealthState.ERROR)
    )

    await asyncio.wait_for(collection.refresh_health(), timeout=5)

    assert collection.enabled_input_modules == {"silent"}


async def test_a_plugin_still_answering_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(player_setup, "PLUGIN_CALL_TIMEOUT_S", 0.01)
    held = _HeldPlugin()
    collection = _collection(held=held)

    await collection.refresh_health()
    await asyncio.gather(collection.refresh_health(), collection.refresh_health())
    assert held.asked == 1

    held.release.set()
    await asyncio.sleep(0.01)
    await collection.refresh_health()
    assert held.asked == 2


async def test_a_failure_after_the_wait_is_never_logged(monkeypatch, caplog):
    monkeypatch.setattr(player_setup, "PLUGIN_CALL_TIMEOUT_S", 0.01)
    held = _HeldPlugin(RuntimeError("401 for https://api.test/?client_id=s3cret"))
    collection = _collection(held=held)
    await collection.refresh_health()

    held.release.set()
    await asyncio.sleep(0.01)
    await collection.refresh_health()
    assert held.asked == 2
    gc.collect()

    assert "RuntimeError" in caplog.text
    assert "s3cret" not in caplog.text


async def test_the_server_follows_a_module_while_it_runs():
    qobuz = _Plugin(ModuleHealthState.ERROR)
    collection = _collection(qobuz=qobuz)
    await collection.refresh_health()
    watching = asyncio.create_task(collection.watch_health(interval_s=0))
    try:
        qobuz.health = ModuleState(state=ModuleHealthState.READY)

        async def reached():
            while "qobuz" not in collection.enabled_input_modules:
                await asyncio.sleep(0)

        await asyncio.wait_for(reached(), timeout=5)
    finally:
        watching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watching


class _FakeConfig(ModuleConfig):
    name: str = Field(default="fake", title="Fake", frozen=True, exclude=True)


class _FakePlugin(InputModulePlugin):
    PLUGIN_ID = "fake"
    REQUIRES_SDK = f"=={SDK_VERSION}"
    CONFIG_MODEL = _FakeConfig
    live = ModuleState(state=ModuleHealthState.ERROR, message=NO_KEY)

    async def setup(self, context):
        self._interface = _Module("fake")

    def get_interface(self):
        return self._interface

    async def get_state(self) -> ModuleState:
        return self.live


@pytest.fixture
async def client(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        modules, "_scan_entry_points", lambda: iter([("fake", _FakePlugin)])
    )
    app = await server.create_app(
        str(tmp_path / "overrides.json"), KalinkaConfig(), {}
    )
    async with running(app) as client:
        yield client


def _browsable(listing) -> list[str]:
    return [EntityId.from_string(item["id"]).source for item in listing.json()["items"]]


async def test_a_module_in_error_from_boot_offers_no_catalogs(client):
    root = await client.get("/browse")
    catalog = await client.get("/browse/kalinka:fake:catalog:root")
    listed = (await client.get("/server/modules")).json()["input_modules"]

    assert "fake" not in _browsable(root)
    assert catalog.status_code == 404
    fake = next(entry for entry in listed if entry["name"] == "fake")
    assert (fake["state"], fake["error_message"]) == ("error", NO_KEY)


async def test_reading_the_modules_lets_a_recovered_module_be_browsed(
    client, monkeypatch
):
    monkeypatch.setattr(
        _FakePlugin, "live", ModuleState(state=ModuleHealthState.READY)
    )

    listed = (await client.get("/server/modules")).json()["input_modules"]
    root = await client.get("/browse")

    assert next(e for e in listed if e["name"] == "fake")["state"] == "ready"
    assert "fake" in _browsable(root)


async def test_a_module_in_error_still_serves_its_content(client):
    reached = await client.get("/content/fake/track-1")
    unknown = await client.get("/content/absent/track-1")

    assert reached.status_code == 503
    assert unknown.status_code == 404
