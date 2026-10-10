"""Power-state discovery, startup requests and MusicCast event dispatch."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from kalinka_plugin_sdk.datamodel import VolumeBackend
from kalinka_plugin_sdk.ext_device import DeviceVolume
from kalinka_plugin_sdk.ext_device_events import (
    DevicePowerStateChangedEvent,
    VolumeChangedEvent,
)

from kalinka_plugin_musiccast.config_model import KalinkaPluginMusiccastConfig
from kalinka_plugin_musiccast.musiccast import KalinkaPluginMusiccastDevice


@pytest.fixture
def device():
    """Create a minimal KalinkaPluginMusiccastDevice with mocked emitter."""
    config = KalinkaPluginMusiccastConfig(
        connected_input="netusb",
        zone_name="main",
    )
    emitter = MagicMock()
    listener = MagicMock()
    dev = KalinkaPluginMusiccastDevice(config, emitter, listener)

    # Initialise the fields that get_ready() would normally set
    dev.volume = DeviceVolume(max_volume=60, current_volume=30, volume_gain=0)
    dev._device_power_on = False
    return dev


async def test_initial_standby_does_not_stop_new_playback(device):
    device._get_status = AsyncMock(
        return_value={
            "power": "standby", "input": "netusb", "volume": 30, "max_volume": 60,
        }
    )
    await device.get_ready()
    assert not any(
        isinstance(c.args[0], DevicePowerStateChangedEvent)
        for c in device.event_emitter.dispatch.call_args_list
    )
    device.event_emitter.dispatch.assert_called_once_with(
        VolumeChangedEvent(volume=device.volume)
    )
    assert device.volume.supported


@pytest.mark.parametrize(
    "was_on, reported, expected",
    [(False, "on", True), (True, "standby", False)],
)
async def test_discovery_still_reports_real_power_transitions(
    device, was_on, reported, expected
):
    device._device_power_on = was_on
    device._get_status = AsyncMock(
        return_value={
            "power": reported, "input": "netusb", "volume": 30, "max_volume": 60,
        }
    )
    await device.get_ready()
    device.event_emitter.dispatch.assert_any_call(
        DevicePowerStateChangedEvent(power_on=expected)
    )


async def test_power_requested_before_discovery_is_applied_when_ready(device):
    device._get_status = AsyncMock(
        return_value={
            "power": "standby", "input": "netusb", "volume": 30, "max_volume": 60,
        }
    )
    device._request_musiccast = AsyncMock()
    device._set_input = AsyncMock()
    await device.power_on()
    device._request_musiccast.assert_not_called()
    await device.get_ready()
    device._request_musiccast.assert_awaited_once_with("/main/setPower?power=on")
    device._set_input.assert_awaited_once()
    device.event_emitter.dispatch.assert_any_call(
        DevicePowerStateChangedEvent(power_on=True)
    )
    assert not device._pending_power_on


async def test_power_off_cancels_pre_discovery_power_request(device):
    await device.power_on()
    await device.power_off()
    assert not device._pending_power_on


@pytest.mark.unit
async def test_standby_dispatches_power_off(device):
    """Standby event while device is 'on' dispatches power_on=False."""
    device._device_power_on = True
    await device._handle_event({"main": {"power": "standby"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=False)
    )
    assert device._device_power_on is False


@pytest.mark.unit
async def test_input_switch_dispatches_power_off(device):
    """Wrong-input event while device is 'on' dispatches power_on=False."""
    device._device_power_on = True
    await device._handle_event({"main": {"input": "hdmi1"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=False)
    )
    assert device._device_power_on is False


@pytest.mark.unit
async def test_power_on_correct_input_dispatches_power_on(device):
    """Power-on event with correct input dispatches power_on=True."""
    device._device_power_on = False
    await device._handle_event({"main": {"power": "on", "input": "netusb"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=True)
    )
    assert device._device_power_on is True


@pytest.mark.unit
async def test_power_on_no_input_dispatches_power_on(device):
    """Power-on event without input field dispatches power_on=True."""
    device._device_power_on = False
    await device._handle_event({"main": {"power": "on"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=True)
    )
    assert device._device_power_on is True


@pytest.mark.unit
async def test_power_on_wrong_input_dispatches_power_off(device):
    """Power-on event with wrong input dispatches power_on=False (input check takes priority)."""
    device._device_power_on = True
    await device._handle_event({"main": {"power": "on", "input": "hdmi1"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=False)
    )
    assert device._device_power_on is False


@pytest.mark.unit
async def test_no_duplicate_power_on_events(device):
    """Repeated power-on events do not dispatch duplicate events."""
    device._device_power_on = False
    await device._handle_event({"main": {"power": "on"}})
    await device._handle_event({"main": {"power": "on"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=True)
    )


@pytest.mark.unit
async def test_no_duplicate_power_off_events(device):
    """Repeated standby events do not dispatch duplicate events."""
    device._device_power_on = True
    await device._handle_event({"main": {"power": "standby"}})
    await device._handle_event({"main": {"power": "standby"}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=False)
    )


@pytest.mark.unit
async def test_volume_only_event_no_power_dispatch(device):
    """Volume-only events do not trigger any power-state dispatch."""
    device._device_power_on = False
    await device._handle_event({"main": {"volume": 30}})
    device.event_emitter.dispatch.assert_not_called()


@pytest.mark.unit
async def test_wrong_zone_ignored(device):
    """Events for a different zone are ignored; no dispatch occurs."""
    device._device_power_on = True
    await device._handle_event({"zone2": {"power": "standby"}})
    device.event_emitter.dispatch.assert_not_called()
    assert device._device_power_on is True


@pytest.mark.unit
async def test_both_standby_and_volume(device):
    """Standby combined with volume in same event dispatches power_on=False."""
    device._device_power_on = True
    await device._handle_event({"main": {"power": "standby", "volume": 20}})
    device.event_emitter.dispatch.assert_called_once_with(
        DevicePowerStateChangedEvent(power_on=False)
    )
    assert device._device_power_on is False


@pytest.mark.unit
async def test_an_unready_device_still_names_where_its_volume_is_applied(device):
    """The amplifier attenuates in its own hardware whether or not we have
    reached it yet — a client weighing bit-perfection must not read the
    silence of a device that has not answered as an unknown backend."""
    device.ready = False
    assert (await device.get_volume()).backend is VolumeBackend.HARDWARE


@pytest.mark.unit
async def test_going_unreachable_keeps_the_backend(device):
    """_mark_unavailable rebuilds the volume; the backend is a property of the
    amplifier, not of whether we can currently talk to it."""
    device.volume = DeviceVolume(
        max_volume=60,
        current_volume=30,
        volume_gain=0,
        supported=True,
        backend=VolumeBackend.HARDWARE,
    )
    device._mark_unavailable()

    assert device.volume.supported is False
    assert device.volume.backend is VolumeBackend.HARDWARE


@pytest.mark.unit
async def test_going_unreachable_is_not_a_power_off(device):
    """A power-off stops playback; an amplifier that missed a request has not
    been switched off."""
    device.volume = DeviceVolume(
        max_volume=60, current_volume=30, volume_gain=0, supported=True
    )
    device._device_power_on = True
    device._mark_unavailable()

    dispatched = [c.args[0] for c in device.event_emitter.dispatch.call_args_list]
    assert not any(isinstance(e, DevicePowerStateChangedEvent) for e in dispatched)
    assert device._device_power_on is True
