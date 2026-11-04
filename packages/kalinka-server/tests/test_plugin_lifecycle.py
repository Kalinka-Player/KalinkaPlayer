"""A plugin is set up and taken down while the server runs: by its on/off
switch, by a settings change it does not take in itself, by its package
being updated or removed. Clients stop being offered it first, the output
it holds and what plays from it are taken back, calls under way drain, it
is told to shut down, and only then is it forgotten or set up again.
"""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import Field

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk import ModuleHealthState
from kalinka_plugin_sdk import __version__ as SDK_VERSION
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState
from kalinka_plugin_sdk.direct_playback import RevokeReason
from kalinka_plugin_sdk.events import PlayQueueEvent, PlayQueueEventType, PlayQueueState
from kalinka_plugin_sdk.inputmodule import InputModule, SourceUnavailableError
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import InputModulePlugin, OutputDevicePlugin

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.module_timeout import TimeLimitedInputModule
from kalinka_server.player_setup import PreparedModuleCollection, PreparedPlugin, modules
from kalinka_server.plugin_lifecycle import RESTARTING, PluginLifecycle, _purge_imports
from kalinka_server.scoped_listener import ScopedListener
from tests.app_harness import isolate_app, running


class _Module(InputModule):
    def module_name(self) -> str:
        return "fake"

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        return None


class _Config(ModuleConfig):
    name: str = Field(default="fake", title="Fake", frozen=True, exclude=True)
    key: str = Field(default="", title="Key")


class _Plugin(InputModulePlugin):
    """Logs what the server does to it; its behaviour is set per test."""

    PLUGIN_ID = "fake"
    REQUIRES_SDK = f"=={SDK_VERSION}"
    CONFIG_MODEL = _Config
    log: list[str] = []
    takes_in = False
    hangs_on_shutdown = False
    leaves_a_subscription = False
    needs_a_key = False

    @classmethod
    def reset(cls):
        cls.log = []
        cls.takes_in = cls.hangs_on_shutdown = False
        cls.leaves_a_subscription = cls.needs_a_key = False

    async def setup(self, context):
        if type(self).needs_a_key and not context.config.key:
            raise RuntimeError("no key")
        self._context = context
        self._interface = _Module()
        type(self).log.append(f"setup:{context.config.key}")
        if type(self).leaves_a_subscription:
            context.listener.subscribe(
                [PlayQueueEventType.PlaybackStateChanged], lambda event: None
            )

    async def shutdown(self):
        type(self).log.append("shutdown")
        if type(self).hangs_on_shutdown:
            await asyncio.Event().wait()

    def get_interface(self):
        return self._interface

    async def config_changed(self, changed):
        type(self).log.append("changed:" + ",".join(sorted(changed)))
        return True if type(self).takes_in else None


class _Receiver(_Plugin):
    """Set up while switched off, as a receiver that watches for pairing is."""

    SETUP_WHEN_DISABLED = True


class _ActingConfig(_Config):
    pair: bool = Field(
        default=False,
        title="Pair now",
        json_schema_extra={"one_shot": True, "apply_immediately": True},
    )


class _RebuildingConfig(_Config):
    rebuild: bool = Field(
        default=False, title="Rebuild", json_schema_extra={"one_shot": True}
    )


class _Rebuilding(_Plugin):
    """Acts on a one-shot when it is set up."""

    CONFIG_MODEL = _RebuildingConfig

    async def setup(self, context):
        await super().setup(context)
        type(self).log.append(f"rebuild:{context.config.rebuild}")


class _Acting(_Plugin):
    """Acts on a one-shot at save time."""

    CONFIG_MODEL = _ActingConfig

    async def config_changed(self, changed):
        type(self).log.append(f"pair:{self._context.config.pair}")
        return True


@pytest.fixture(autouse=True)
def _fresh_plugin():
    _Plugin.reset()
    yield
    _Plugin.reset()


async def _until(condition, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the server never got there")


# ---------------------------------------------------------------------------
# Through the server


async def _app(monkeypatch, tmp_path, plugin_class=_Plugin):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        modules, "_scan_entry_points", lambda: iter([("fake", plugin_class)])
    )
    return await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})


@pytest.fixture
async def app(monkeypatch, tmp_path):
    return await _app(monkeypatch, tmp_path)


@pytest.fixture
async def client(app):
    async with running(app) as client:
        yield client


async def _save(client, changes: dict) -> None:
    version = (await client.get("/server/config")).json()["schema_version"]
    answer = await client.put(
        "/server/config", json={"schema_version": version, "changes": changes}
    )
    assert answer.status_code == 200, answer.text


async def _listed(client) -> dict:
    listed = (await client.get("/server/modules")).json()["input_modules"]
    return next(entry for entry in listed if entry["name"] == "fake")


async def test_switching_a_module_off_shuts_it_down_and_on_sets_it_up_again(client):
    assert _Plugin.log == ["setup:"]

    await _save(client, {"input_modules.fake.enabled": False})
    await _until(lambda: _Plugin.log[-1] == "shutdown")

    assert (await _listed(client))["state"] == "disabled"
    assert (await client.get("/content/fake/anything")).status_code == 404
    # Its settings are still there to edit.
    assert "input_modules.fake.key" in (await client.get("/server/config")).json()["values"]

    await _save(client, {"input_modules.fake.enabled": True})
    await _until(lambda: _Plugin.log[-1] == "setup:")

    assert (await _listed(client))["state"] == "ready"
    assert _Plugin.log == ["setup:", "shutdown", "setup:"]


async def test_a_setting_the_plugin_takes_in_leaves_it_running(client):
    _Plugin.takes_in = True

    await _save(client, {"input_modules.fake.key": "abc"})
    await _until(lambda: _Plugin.log[-1] == "changed:key")
    await asyncio.sleep(0.1)

    assert _Plugin.log == ["setup:", "changed:key"]


async def test_a_setting_the_plugin_does_not_take_in_sets_it_up_again_on_it(client):
    await _save(client, {"input_modules.fake.key": "abc"})
    await _until(lambda: _Plugin.log[-1] == "setup:abc")

    assert _Plugin.log == ["setup:", "changed:key", "shutdown", "setup:abc"]
    assert (await _listed(client))["state"] == "ready"


async def test_a_plugin_that_failed_to_set_up_gets_another_chance_on_a_save(
    monkeypatch, tmp_path
):
    _Plugin.needs_a_key = True
    app = await _app(monkeypatch, tmp_path)
    async with running(app) as client:
        assert (await _listed(client))["state"] == "error"

        await _save(client, {"input_modules.fake.key": "abc"})
        await _until(lambda: _Plugin.log[-1:] == ["setup:abc"])

        assert (await _listed(client))["state"] == "ready"


async def test_a_receiver_set_up_while_off_takes_the_switch_in_itself(monkeypatch, tmp_path):
    _Plugin.takes_in = True
    app = await _app(monkeypatch, tmp_path, _Receiver)
    async with running(app) as client:
        await _save(client, {"input_modules.fake.enabled": False})
        await _until(lambda: _Plugin.log[-1] == "changed:enabled")
        await asyncio.sleep(0.1)
        assert _Plugin.log == ["setup:", "changed:enabled"]

        # Switched off, it still runs: taking it down shuts it down.
        await app.state.plugin_lifecycle.unload("fake", gone=True)
        assert _Plugin.log[-1] == "shutdown"


async def test_a_one_shot_acted_on_at_save_time_is_armed_for_the_plugin_only(
    monkeypatch, tmp_path
):
    app = await _app(monkeypatch, tmp_path, _Acting)
    async with running(app) as client:
        await _save(client, {"input_modules.fake.pair": True})
        await _until(lambda: _Plugin.log[-1] == "pair:True")
        await asyncio.sleep(0.1)

        values = (await client.get("/server/config")).json()["values"]
        assert values["input_modules.fake.pair"] is False
        saved = json.loads((tmp_path / "overrides.json").read_text())
        assert "input_modules.fake.pair" not in saved


async def test_a_one_shot_fires_once_when_the_plugin_is_set_up_again_on_it(
    monkeypatch, tmp_path
):
    app = await _app(monkeypatch, tmp_path, _Rebuilding)
    async with running(app) as client:
        assert _Plugin.log == ["setup:", "rebuild:False"]
        await _save(client, {"input_modules.fake.rebuild": True})
        await _until(lambda: _Plugin.log[-1] == "rebuild:True")
        await asyncio.sleep(0.1)

        values = (await client.get("/server/config")).json()["values"]
        assert values["input_modules.fake.rebuild"] is False
        saved = json.loads((tmp_path / "overrides.json").read_text())
        assert "input_modules.fake.rebuild" not in saved

        await _save(client, {"input_modules.fake.key": "k"})
        await _until(lambda: _Plugin.log[-1] == "rebuild:False")


async def test_content_of_a_module_being_set_up_again_is_retried_later(client):
    modules.prepared_input_modules["fake"].mounted = False

    answer = await client.get("/content/fake/anything")

    assert answer.status_code == 503
    assert answer.headers["Retry-After"] == "5"


async def test_a_module_whose_package_is_gone_is_forgotten(client, app):
    version_before = app.state.schema_version

    await app.state.plugin_lifecycle.unload("fake", gone=True)

    assert _Plugin.log == ["setup:", "shutdown"]
    listed = (await client.get("/server/modules")).json()["input_modules"]
    assert "fake" not in [entry["name"] for entry in listed]
    assert (await client.get("/content/fake/anything")).status_code == 404
    assert app.state.schema_version != version_before


async def test_a_module_loaded_from_a_new_package_is_listed(client, app):
    await app.state.plugin_lifecycle.unload("fake", gone=True)

    await app.state.plugin_lifecycle.load("fake", purge=False)

    assert (await _listed(client))["state"] == "ready"
    assert _Plugin.log == ["setup:", "shutdown", "setup:"]


# ---------------------------------------------------------------------------
# The order of an unload, against fakes


def _bus() -> EventBus:
    return EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )


class _Device(OutputDevicePlugin):
    PLUGIN_ID = "amp"
    REQUIRES_SDK = f"=={SDK_VERSION}"
    CONFIG_MODEL = _Config

    async def setup(self, context):
        pass

    async def shutdown(self):
        _Plugin.log.append("amp:shutdown")


@pytest.fixture
def bus():
    bus = _bus()
    yield bus
    bus.close()


@pytest.fixture
def fakes():
    return SimpleNamespace(
        playqueue=SimpleNamespace(drop_source=AsyncMock(), source_returned=AsyncMock()),
        arbiter=SimpleNamespace(revoke_plugin=AsyncMock()),
        router=SimpleNamespace(resync=AsyncMock()),
        replaygain=SimpleNamespace(refresh=AsyncMock()),
        storage=SimpleNamespace(refresh=AsyncMock()),
        automation=SimpleNamespace(forget=Mock()),
        republish=AsyncMock(),
        rebuild_schema=Mock(),
        packages_changed=AsyncMock(),
    )


def _lifecycle(collection, fakes, **kwargs) -> PluginLifecycle:
    return PluginLifecycle(
        collection,
        overrides={},
        playqueue=fakes.playqueue,
        arbiter=fakes.arbiter,
        device_router=fakes.router,
        replaygain=fakes.replaygain,
        storage=fakes.storage,
        device_automation=lambda: fakes.automation,
        republish=fakes.republish,
        rebuild_schema=fakes.rebuild_schema,
        packages_changed=fakes.packages_changed,
        **kwargs,
    )


async def _input_plugin(bus, plugin_class=_Plugin) -> tuple[PreparedModuleCollection, PreparedPlugin]:
    listener = ScopedListener(bus, "fake")
    instance = plugin_class()
    context = SimpleNamespace(
        config=_Config(), listener=listener, plugin_id="fake"
    )
    await instance.setup(context)
    prepared = PreparedPlugin(
        plugin_class=plugin_class,
        plugin_instance=instance,
        health_state=ModuleHealthState.READY,
        plugin_context=context,
        interface=TimeLimitedInputModule(instance.get_interface(), "fake"),
    )
    return PreparedModuleCollection(prepared_input_modules={"fake": prepared}), prepared


async def test_an_unload_takes_the_output_and_the_queue_back_before_shutting_down(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    order: list[str] = []
    fakes.arbiter.revoke_plugin.side_effect = lambda *a, **k: order.append("revoke")
    fakes.playqueue.drop_source.side_effect = lambda *a, **k: order.append("drop")
    interface = prepared.interface

    await _lifecycle(collection, fakes).unload("fake", gone=False)

    assert order == ["revoke", "drop"]
    assert _Plugin.log == ["setup:", "shutdown"]
    fakes.arbiter.revoke_plugin.assert_awaited_once_with("fake", RevokeReason.PLUGIN_UNLOADED)
    fakes.playqueue.drop_source.assert_awaited_once_with("fake", gone=False)
    with pytest.raises(SourceUnavailableError, match="is restarting"):
        await interface.browse(None)
    # Left as a switched-off plugin: a fresh instance, never set up, with its settings.
    assert prepared.health_state == ModuleHealthState.DISABLED
    assert prepared.plugin_instance is not None and prepared.interface is None
    assert prepared.mounted
    assert fakes.republish.await_count == 2
    fakes.rebuild_schema.assert_called_once()


async def test_an_unload_before_a_reload_keeps_the_module_unmounted(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    lifecycle = _lifecycle(collection, fakes)

    await lifecycle._unload("fake", gone=False, resuming=True)

    assert not prepared.mounted
    assert collection.set_up_input_module("fake") is None


async def test_a_module_being_set_up_again_says_so_meanwhile(bus, fakes):
    collection, prepared = await _input_plugin(bus)

    await _lifecycle(collection, fakes)._unload("fake", gone=False, resuming=True)

    assert prepared.state.state == ModuleHealthState.WARNING
    assert prepared.state.message == RESTARTING


async def test_a_plugin_taken_down_or_set_up_requeues_the_optional_packages(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    lifecycle = _lifecycle(collection, fakes)

    await lifecycle.unload("fake", gone=False)
    await _until(lambda: fakes.packages_changed.await_count == 1)

    collection.prepare_one = AsyncMock(return_value=prepared)
    await lifecycle.load("fake", purge=False)
    await _until(lambda: fakes.packages_changed.await_count == 2)


async def test_a_save_while_the_plugin_is_down_for_an_update_waits_for_it(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    lifecycle = _lifecycle(collection, fakes)
    await lifecycle.suspend("fake")
    collection.prepare_one = AsyncMock(return_value=prepared)

    await lifecycle.settings_changed("fake", frozenset({"anything"}))

    collection.prepare_one.assert_not_awaited()
    assert not prepared.mounted
    assert prepared.state.message == RESTARTING


async def test_a_plugin_given_up_on_is_listed_in_error_and_reachable(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    lifecycle = _lifecycle(collection, fakes)
    await lifecycle.suspend("fake")

    await lifecycle.abandon("fake", "The update did not finish.")

    assert prepared.mounted
    assert prepared.state.state == ModuleHealthState.ERROR
    assert prepared.state.message == "The update did not finish."
    assert collection.set_up_input_module("fake") is None


async def test_a_reload_keeps_the_modules_place_in_the_listing(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    _, last = await _input_plugin(bus)
    collection.prepared_input_modules["last"] = last
    collection.prepare_one = AsyncMock(return_value=prepared)

    lifecycle = _lifecycle(collection, fakes)
    await lifecycle.suspend("fake")
    await lifecycle.load("fake", purge=False)

    assert list(collection.prepared_input_modules) == ["fake", "last"]


async def test_a_reload_that_fails_leaves_the_module_reachable_and_in_error(bus, fakes):
    collection, prepared = await _input_plugin(bus)
    lifecycle = _lifecycle(collection, fakes)
    await lifecycle._unload("fake", gone=False, resuming=True)
    collection.prepare_one = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError):
        await lifecycle._load("fake", purge=False)

    assert prepared.mounted
    assert prepared.state.state == ModuleHealthState.ERROR
    assert "RuntimeError" in prepared.state.message
    assert collection.set_up_input_module("fake") is None


async def test_a_shutdown_that_hangs_is_given_up_on(bus, fakes, caplog):
    collection, _prepared = await _input_plugin(bus)
    _Plugin.hangs_on_shutdown = True

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            _lifecycle(collection, fakes, shutdown_timeout_s=0.05).unload("fake", gone=False),
            timeout=2,
        )

    assert "did not shut down cleanly (TimeoutError)" in caplog.text


async def test_what_the_plugin_left_subscribed_is_closed(bus, fakes, caplog):
    _Plugin.leaves_a_subscription = True
    collection, prepared = await _input_plugin(bus)

    with caplog.at_level(logging.WARNING):
        await _lifecycle(collection, fakes).unload("fake", gone=False)

    assert "fake left 1 bus subscription(s) open" in caplog.text
    assert prepared.plugin_context.listener.revoke_all() == 0


async def test_a_gone_package_leaves_nothing_behind(bus, fakes):
    collection, _prepared = await _input_plugin(bus)

    await _lifecycle(collection, fakes).unload("fake", gone=True)

    assert collection.prepared("fake") is None
    fakes.playqueue.drop_source.assert_awaited_once_with("fake", gone=True)
    fakes.storage.refresh.assert_awaited_once()


async def test_a_device_being_unloaded_is_forgotten_by_automation(bus, fakes):
    instance = _Device()
    context = SimpleNamespace(
        config=_Config(name="amp"), listener=ScopedListener(bus, "amp"), plugin_id="amp"
    )
    prepared = PreparedPlugin(
        plugin_class=_Device,
        plugin_instance=instance,
        health_state=ModuleHealthState.READY,
        plugin_context=context,
        interface=None,
    )
    collection = PreparedModuleCollection(prepared_devices={"amp": prepared})

    await _lifecycle(collection, fakes).unload("amp", gone=False)

    fakes.automation.forget.assert_called_once_with("amp")
    fakes.router.resync.assert_awaited()
    fakes.replaygain.refresh.assert_awaited()
    fakes.playqueue.drop_source.assert_not_awaited()
    assert _Plugin.log == ["amp:shutdown"]


async def test_unloads_are_one_at_a_time(bus, fakes):
    collection, _prepared = await _input_plugin(bus)
    _Plugin.hangs_on_shutdown = True
    lifecycle = _lifecycle(collection, fakes, shutdown_timeout_s=0.1)

    first = asyncio.create_task(lifecycle.unload("fake", gone=False))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(lifecycle.unload("fake", gone=False))
    await asyncio.sleep(0.01)

    assert not first.done() and not second.done()
    await asyncio.gather(first, second)
    assert _Plugin.log.count("shutdown") == 1


def test_purging_imports_drops_the_plugins_package_and_keeps_the_sdk():
    package = type(sys)("fakeplug")
    submodule = type(sys)("fakeplug.inner")
    sys.modules["fakeplug"] = package
    sys.modules["fakeplug.inner"] = submodule
    plugin_class = type("Plugin", (), {"__module__": "fakeplug.inner"})
    try:
        _purge_imports(plugin_class)

        assert "fakeplug" not in sys.modules and "fakeplug.inner" not in sys.modules
        assert "kalinka_plugin_sdk" in sys.modules
    finally:
        sys.modules.pop("fakeplug", None)
        sys.modules.pop("fakeplug.inner", None)
