"""Tests for DeviceAutomation module."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk import DeviceVolume
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.events import (
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
    PlaybackStateChangedEvent,
)
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    DevicePowerStateChangedEvent,
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
)

from kalinka_server.config_model import DeviceAutomationConfig
from kalinka_server.device_automation import DeviceAutomation


def make_playqueue_state(state: PlayerStateEnum) -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(state=state),
        track_list=[],
        playback_mode=PlaybackMode(shuffle=False, repeat_single=False, repeat_all=False),
    )


def make_ext_device_state() -> ExtDeviceState:
    return ExtDeviceState(
        power_on=False,
        volume=DeviceVolume(max_volume=60, current_volume=30, volume_gain=0),
    )


@pytest.fixture
def playqueue_eventbus():
    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=make_playqueue_state(PlayerStateEnum.STOPPED)
    )
    yield bus
    bus.close()


@pytest.fixture
def ext_device_eventbus():
    bus = EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent](
        initial_state=make_ext_device_state()
    )
    yield bus
    bus.close()


@pytest.fixture
def mock_device():
    device = MagicMock()
    device.supported_functions.return_value = {
        SupportedFunction.POWER_ON,
        SupportedFunction.POWER_OFF,
        SupportedFunction.IS_POWER_ON,
    }
    device.is_power_on = AsyncMock(return_value=False)
    device.power_on = AsyncMock()
    device.power_off = AsyncMock()
    return device


@pytest.fixture
def mock_playqueue():
    pq = AsyncMock()
    pq.get_playback_state = AsyncMock(
        return_value=PlaybackState(state=PlayerStateEnum.STOPPED)
    )
    pq.stop = AsyncMock()
    return pq


@pytest.fixture
def config():
    return DeviceAutomationConfig(
        auto_power_on=True,
        auto_power_off=True,
        auto_off_timeout_seconds=1,
    )


def _pending(automation) -> bool:
    return any(not t.done() for t in automation._auto_off_tasks.values())


class FakeRouter:
    """Stands in for OutputDeviceRouter: which module owns the output, and
    the device behind it. Both mutable, to model a renderer switch."""

    def __init__(self, device=None, name="kalinka-renderer"):
        self.device = device
        self.name = name

    def current(self):
        return self.device

    def current_name(self):
        return self.name


async def make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, device=None):
    automation = DeviceAutomation(
        config=config,
        playqueue=mock_playqueue,
        playqueue_eventbus=playqueue_eventbus,
        ext_device_eventbus=ext_device_eventbus,
        router=FakeRouter(device),
    )
    await automation.start()
    # Give the listeners time to subscribe before we dispatch events
    await asyncio.sleep(0.05)
    return automation


async def test_power_off_event_stops_playing(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """DevicePowerStateChangedEvent(power_on=False) stops playback when state=PLAYING."""
    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.PLAYING)
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
        await asyncio.sleep(0.1)
        mock_playqueue.stop.assert_called_once()
    finally:
        await automation.shutdown()


async def test_power_off_event_stops_paused(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """DevicePowerStateChangedEvent(power_on=False) stops playback when state=PAUSED."""
    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.PAUSED)
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
        await asyncio.sleep(0.1)
        mock_playqueue.stop.assert_called_once()
    finally:
        await automation.shutdown()


async def test_power_off_event_noop_when_stopped(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """DevicePowerStateChangedEvent(power_on=False) does not call stop when already STOPPED."""
    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.STOPPED)
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
        await asyncio.sleep(0.1)
        mock_playqueue.stop.assert_not_called()
    finally:
        await automation.shutdown()


async def test_power_on_event_no_action(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """DevicePowerStateChangedEvent(power_on=True) does not stop playback."""
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=True))
        await asyncio.sleep(0.1)
        mock_playqueue.stop.assert_not_called()
    finally:
        await automation.shutdown()


async def test_auto_power_on_on_playing(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """PlaybackStateChangedEvent(PLAYING) causes device.power_on() to be called."""
    mock_device.is_power_on.return_value = False
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(0.1)
        mock_device.power_on.assert_called_once()
    finally:
        await automation.shutdown()


async def test_power_follows_the_currently_resolved_device(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """The device is resolved per action: re-delegating a renderer's output
    moves power control without restarting automation."""
    other = MagicMock()
    other.supported_functions.return_value = {SupportedFunction.POWER_ON}
    other.power_on = AsyncMock()
    router = FakeRouter(mock_device, name="musiccast")

    automation = DeviceAutomation(
        config=config,
        playqueue=mock_playqueue,
        playqueue_eventbus=playqueue_eventbus,
        ext_device_eventbus=ext_device_eventbus,
        router=router,
    )
    await automation.start()
    await asyncio.sleep(0.05)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(0.1)
        mock_device.power_on.assert_called_once()

        router.device, router.name = other, "kalinka-renderer"
        mock_playqueue.get_playback_state.return_value = PlaybackState(
            state=PlayerStateEnum.STOPPED
        )
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.05)
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(0.1)

        other.power_on.assert_called_once()
        # The device that is no longer in charge is left alone.
        mock_device.power_on.assert_called_once()
    finally:
        await automation.shutdown()


async def test_switching_renderers_still_powers_down_the_one_left_behind(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """Playback moving to another renderer must not cancel the previous
    device's power-off: the amp it was using is now idle."""
    renderer = MagicMock()
    renderer.supported_functions.return_value = {SupportedFunction.POWER_ON}
    renderer.power_on = AsyncMock()
    router = FakeRouter(mock_device, name="musiccast")

    automation = DeviceAutomation(
        config=config,
        playqueue=mock_playqueue,
        playqueue_eventbus=playqueue_eventbus,
        ext_device_eventbus=ext_device_eventbus,
        router=router,
    )
    await automation.start()
    await asyncio.sleep(0.05)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(0.1)
        mock_device.power_on.assert_called_once()
        mock_device.is_power_on.return_value = True  # it is on now

        # Selecting another renderer stops playback here...
        mock_playqueue.get_playback_state.return_value = PlaybackState(
            state=PlayerStateEnum.STOPPED
        )
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.05)

        # ...and resumes on the new one well inside the grace period.
        router.device, router.name = renderer, "kalinka-renderer"
        mock_playqueue.get_playback_state.return_value = PlaybackState(
            state=PlayerStateEnum.PLAYING
        )
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(0.1)

        # The amp left behind powers down anyway, and playback is untouched.
        await asyncio.sleep(1.2)
        mock_device.power_off.assert_called_once()
        mock_playqueue.stop.assert_not_called()
    finally:
        await automation.shutdown()


async def test_switching_renderers_on_the_same_device_does_not_power_cycle(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    router = FakeRouter(mock_device, name="musiccast")
    automation = DeviceAutomation(
        config=config,
        playqueue=mock_playqueue,
        playqueue_eventbus=playqueue_eventbus,
        ext_device_eventbus=ext_device_eventbus,
        router=router,
    )
    try:
        await automation._handle_playback_state_change(
            PlaybackState(state=PlayerStateEnum.PLAYING)
        )
        mock_device.is_power_on.return_value = True
        await automation._handle_playback_state_change(
            PlaybackState(state=PlayerStateEnum.STOPPED)
        )
        await automation._handle_playback_state_change(
            PlaybackState(state=PlayerStateEnum.BUFFERING)
        )
        await asyncio.sleep(0)

        mock_device.power_on.assert_called_once()
        mock_device.power_off.assert_not_called()
        assert not _pending(automation)
    finally:
        await automation.shutdown()


async def test_auto_power_on_on_buffering(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """PlaybackStateChangedEvent(BUFFERING) causes device.power_on() to be called."""
    mock_device.is_power_on.return_value = False
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.BUFFERING))
        )
        await asyncio.sleep(0.1)
        mock_device.power_on.assert_called_once()
    finally:
        await automation.shutdown()


async def test_auto_off_timer_fires_after_stop(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """After STOPPED event, device powers off after the grace-period timeout."""
    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.STOPPED)
    mock_device.is_power_on.return_value = True
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        # Wait longer than the 1 s timeout
        await asyncio.sleep(1.5)
        mock_device.power_off.assert_called_once()
    finally:
        await automation.shutdown()


async def test_auto_off_timer_cancelled_on_resume(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """Auto-off timer is cancelled when playback resumes before the timeout."""
    mock_device.is_power_on.return_value = True
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.2)

        # Resume before timeout
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(1.3)  # well past the 1 s timeout

        mock_device.power_off.assert_not_called()
    finally:
        await automation.shutdown()


async def test_auto_off_timer_not_restarted_on_second_pause(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """Timer is not reset when transitioning from STOPPED to PAUSED (timer already running)."""
    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.PAUSED)
    mock_device.is_power_on.return_value = True
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.2)

        timer_task_after_first = automation._auto_off_tasks.get('kalinka-renderer')

        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PAUSED))
        )
        await asyncio.sleep(0.1)

        # The same task object should still be running (not replaced)
        assert automation._auto_off_tasks.get('kalinka-renderer') is timer_task_after_first

        # Let the timer fire
        await asyncio.sleep(1.0)
        mock_device.power_off.assert_called_once()
    finally:
        await automation.shutdown()


async def test_listener_restarts_after_error(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """_device_event_listener restarts after a non-cancellation exception."""
    call_count = 0
    original_on_power_off = DeviceAutomation._on_device_power_off

    async def failing_then_working(self):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated listener failure")
        await original_on_power_off(self)

    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.PLAYING)

    with patch.object(DeviceAutomation, "_on_device_power_off", failing_then_working):
        automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
        try:
            # First dispatch triggers the error; the retry loop then sleeps 1 s before restarting
            ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
            # Wait long enough for the 1 s sleep + listener to re-subscribe
            await asyncio.sleep(1.5)

            # Second dispatch should be handled normally after restart
            ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
            await asyncio.sleep(0.2)

            assert call_count >= 2
            mock_playqueue.stop.assert_called_once()
        finally:
            await automation.shutdown()


async def test_power_off_event_does_not_start_auto_off_timer(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """After external power-off, the STOPPED event must NOT start the auto-off timer."""
    mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.PLAYING)
    mock_device.is_power_on.return_value = True
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
        await asyncio.sleep(0.1)
        mock_playqueue.stop.assert_called_once()

        # Simulate the STOPPED event that playqueue.stop() would emit
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.1)

        # Timer must not have been created — device is already off
        assert not _pending(automation)
        mock_device.power_off.assert_not_called()
    finally:
        await automation.shutdown()


async def test_power_off_event_cancels_running_auto_off_timer(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """If the auto-off timer is already running when a power-off event arrives, it is cancelled."""
    mock_device.is_power_on.return_value = True
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        # Start the timer via a STOPPED playback event
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.05)
        assert _pending(automation)

        # Now the device powers off externally — timer should be cancelled immediately
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
        await asyncio.sleep(0.05)
        assert not _pending(automation)

        # Wait past the original timeout — still no power_off call
        await asyncio.sleep(1.2)
        mock_device.power_off.assert_not_called()
    finally:
        await automation.shutdown()


async def test_auto_off_resumes_after_device_powers_back_on(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """After a power-on event clears the flag, the normal auto-off flow resumes."""
    mock_device.is_power_on.return_value = True
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        # Device powers off externally
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=False))
        await asyncio.sleep(0.1)
        assert automation._device_externally_off is True

        # Device powers back on
        ext_device_eventbus.dispatch(DevicePowerStateChangedEvent(power_on=True))
        await asyncio.sleep(0.05)
        assert automation._device_externally_off is False

        # Now a normal STOPPED event should start the auto-off timer
        mock_playqueue.get_playback_state.return_value = PlaybackState(state=PlayerStateEnum.STOPPED)
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.STOPPED))
        )
        await asyncio.sleep(0.05)
        assert _pending(automation)

        # And the device gets powered off after the timeout
        await asyncio.sleep(1.2)
        mock_device.power_off.assert_called_once()
    finally:
        await automation.shutdown()


async def test_a_forgotten_device_is_not_powered_off_by_its_timer(
    config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device
):
    """The module is being unloaded: its auto-off timer would otherwise reach
    an instance that is gone."""
    automation = await make_automation(config, mock_playqueue, playqueue_eventbus, ext_device_eventbus, mock_device)
    try:
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PLAYING))
        )
        await asyncio.sleep(0.1)
        playqueue_eventbus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PAUSED))
        )
        await asyncio.sleep(0.1)
        assert _pending(automation)

        automation.forget("kalinka-renderer")

        await asyncio.sleep(0.05)
        assert not _pending(automation)
        assert automation._in_use is None
        await asyncio.sleep(1.2)
        mock_device.power_off.assert_not_awaited()
    finally:
        await automation.shutdown()
