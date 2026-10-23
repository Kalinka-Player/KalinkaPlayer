"""Delegated volume follows device availability without a renderer switch."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import Field

from kalinka_plugin_sdk import DeviceVolume, ModuleHealthState, ModuleState, paths
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEventType,
    ExtDeviceState,
    VolumeChangedEvent,
)
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import OutputDevicePlugin
from kalinka_server import player_setup, server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.output_device_router import OutputDeviceRouter
from kalinka_server.renderer_prefs import RendererPreferences
from kalinka_server.renderer_sessions import SessionVolumePolicy
from tests.app_harness import isolate_app, running


class _Config(ModuleConfig):
    name: str = Field(default="delegate", frozen=True, exclude=True)
    enabled: bool = True


class _Delegate(OutputDevicePlugin):
    """A controllable output whose live health can change after setup."""

    PLUGIN_ID = "delegate"
    CONFIG_MODEL = _Config
    REQUIRES_SDK = ">=3,<4"
    health = ModuleHealthState.READY
    fail_setup = False

    async def setup(self, context):
        if self.fail_setup:
            raise RuntimeError("Device unavailable")
        self.emitter = context.emitter
        self.volume_reads = 0
        self.emitter.set_initial_state(
            ExtDeviceState(power_on=True, volume=await self.get_volume())
        )

    def get_interface(self):
        return self

    async def get_state(self):
        return ModuleState(state=self.health)

    async def get_volume(self):
        self.volume_reads += 1
        return DeviceVolume(max_volume=100, current_volume=70, supported=True)

    async def set_volume(self, volume):
        pass

    async def power_on(self):
        pass

    async def power_off(self):
        pass

    async def is_power_on(self):
        return True

    def supported_functions(self):
        return [
            SupportedFunction.GET_VOLUME,
            SupportedFunction.SET_VOLUME,
            SupportedFunction.IS_POWER_ON,
        ]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    prefs = RendererPreferences(str(Path(paths.state_dir()) / "renderers.json"))
    prefs.set_selected("renderer", "Living room")
    prefs.set_volume_control("renderer", "delegate")
    monkeypatch.setattr(
        player_setup.modules,
        "_scan_entry_points",
        lambda: iter([(_Delegate.PLUGIN_ID, _Delegate)]),
    )
    monitor = OutputDeviceRouter.monitor

    async def fast_monitor(self):
        await monitor(self, interval_s=0.01)

    monkeypatch.setattr(OutputDeviceRouter, "monitor", fast_monitor)
    return tmp_path


async def _app(tmp_path, overrides=None):
    return await server.create_app(
        str(tmp_path / "overrides.json"), KalinkaConfig(), overrides or {}
    )


def _volume(app):
    return app.state.player_context.ext_device_eventbus.get_snapshot().volume


async def _wait_volume(app, supported):
    bus = app.state.player_context.ext_device_eventbus
    async with bus.stream(list(ExtDeviceEventType)) as events:
        async with asyncio.timeout(2):
            async for _ in events:
                if _volume(app).supported is supported:
                    return


@pytest.mark.parametrize(
    "availability", ["missing", "disabled", "error", "warning", "ready"]
)
async def test_startup_reports_only_an_available_delegate(
    isolated, monkeypatch, availability
):
    if availability == "missing":
        monkeypatch.setattr(
            player_setup.modules, "_scan_entry_points", lambda: iter(())
        )
    elif availability == "error":
        monkeypatch.setattr(_Delegate, "fail_setup", True)
    elif availability == "warning":
        monkeypatch.setattr(_Delegate, "health", ModuleHealthState.WARNING)
    overrides = (
        {"devices.delegate.enabled": False} if availability == "disabled" else {}
    )
    app = await _app(isolated, overrides)
    startup_volume = _volume(app)
    async with running(app):
        assert startup_volume.supported is (availability == "ready")
        assert _volume(app).supported is (availability == "ready")
        if availability == "ready":
            assert _volume(app).current_volume == 70
        else:
            assert app.state.device_router.current() is None
        assert app.state.device_router.session_volume_policy(
            "renderer"
        ) == SessionVolumePolicy(force_fixed_output=True)


async def test_bus_starts_unsupported_before_plugins_report(isolated, monkeypatch):
    initial = []

    def scan():
        initial.append(
            player_setup.modules.player_context.ext_device_eventbus.get_snapshot()
        )
        return iter(())

    monkeypatch.setattr(player_setup.modules, "_scan_entry_points", scan)
    app = await _app(isolated)
    async with running(app):
        assert len(initial) == 1
        assert initial[0].volume.supported is False


@pytest.mark.parametrize(
    "health",
    [ModuleHealthState.ERROR, ModuleHealthState.WARNING, ModuleHealthState.DISABLED],
)
async def test_live_health_loss_and_recovery_reach_existing_clients(isolated, health):
    app = await _app(isolated)
    async with running(app):
        plugin = player_setup.modules.prepared_devices["delegate"].plugin_instance
        plugin.health = health
        await _wait_volume(app, False)
        assert app.state.device_router.current() is None

        plugin.emitter.dispatch(VolumeChangedEvent(volume=await plugin.get_volume()))
        plugin.emitter.set_initial_state(
            ExtDeviceState(power_on=True, volume=await plugin.get_volume())
        )
        assert _volume(app).supported is False

        plugin.health = ModuleHealthState.READY
        await _wait_volume(app, True)
        assert _volume(app).current_volume == 70
        assert app.state.device_router.current() is plugin
        assert app.state.renderer_registry.active_id() == "renderer"
    assert app.state.device_router_task.cancelled()


async def test_removing_and_restoring_delegate_updates_volume(isolated):
    app = await _app(isolated)
    async with running(app):
        prepared = player_setup.modules.prepared_devices.pop("delegate")
        await _wait_volume(app, False)
        prepared.plugin_instance.emitter.dispatch(
            VolumeChangedEvent(volume=await prepared.plugin_instance.get_volume())
        )
        assert _volume(app).supported is False
        player_setup.modules.prepared_devices["delegate"] = prepared
        await _wait_volume(app, True)


async def test_config_disable_and_reenable_resync_before_returning(isolated):
    app = await _app(isolated)
    async with running(app) as client:
        app.state.device_router_task.cancel()
        await asyncio.gather(app.state.device_router_task, return_exceptions=True)
        for enabled in (False, True):
            response = await client.put(
                "/server/config",
                json={
                    "schema_version": app.state.schema_version,
                    "changes": {"devices.delegate.enabled": enabled},
                },
            )
            assert response.status_code == 200, response.text
            assert _volume(app).supported is enabled


async def test_health_probe_failure_can_recover(isolated, monkeypatch):
    app = await _app(isolated)
    async with running(app):
        plugin = player_setup.modules.prepared_devices["delegate"].plugin_instance
        probe = AsyncMock(side_effect=RuntimeError("offline"))
        monkeypatch.setattr(plugin, "get_state", probe)
        await _wait_volume(app, False)
        probe.side_effect = None
        probe.return_value = ModuleState(state=ModuleHealthState.READY)
        await _wait_volume(app, True)


async def test_a_removed_delegate_cannot_publish_an_inflight_recovery(
    isolated, monkeypatch
):
    app = await _app(isolated)
    async with running(app):
        app.state.device_router_task.cancel()
        await asyncio.gather(app.state.device_router_task, return_exceptions=True)
        prepared = player_setup.modules.prepared_devices["delegate"]
        plugin = prepared.plugin_instance
        plugin.health = ModuleHealthState.ERROR
        await app.state.device_router.resync()
        assert _volume(app).supported is False

        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_volume():
            entered.set()
            await release.wait()
            return DeviceVolume(max_volume=100, current_volume=70, supported=True)

        monkeypatch.setattr(plugin, "get_volume", delayed_volume)
        plugin.health = ModuleHealthState.READY
        resync = asyncio.create_task(app.state.device_router.resync())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            del player_setup.modules.prepared_devices["delegate"]
            release.set()
            await asyncio.wait_for(resync, timeout=2)
            assert _volume(app).supported is False
        finally:
            release.set()
            await resync
            player_setup.modules.prepared_devices["delegate"] = prepared


async def test_enabling_a_plugin_that_never_started_keeps_volume_unavailable(isolated):
    app = await _app(isolated, {"devices.delegate.enabled": False})
    async with running(app) as client:
        response = await client.put(
            "/server/config",
            json={
                "schema_version": app.state.schema_version,
                "changes": {"devices.delegate.enabled": True},
            },
        )
        assert response.status_code == 200, response.text
        assert app.state.device_router.current() is None
        assert _volume(app).supported is False


async def test_unchanged_health_does_not_reread_volume_or_publish(
    isolated, monkeypatch
):
    app = await _app(isolated)
    async with running(app):
        plugin = player_setup.modules.prepared_devices["delegate"].plugin_instance
        reads = plugin.volume_reads
        bus = app.state.player_context.ext_device_eventbus
        publish = Mock(wraps=bus.set_initial_state)
        monkeypatch.setattr(bus, "set_initial_state", publish)
        await app.state.device_router.resync(only_if_changed=True)
        assert plugin.volume_reads == reads
        publish.assert_not_called()
        assert bus.get_snapshot().volume.current_volume == 70
