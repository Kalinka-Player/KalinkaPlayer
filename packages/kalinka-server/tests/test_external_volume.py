"""Volume follows an external hold without changing renderer/amp levels."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from kalinka_plugin_sdk.datamodel import (
    DeviceVolume,
    PlaybackControl,
    PlaybackState,
    PlayerStateEnum,
)
from kalinka_plugin_sdk.direct_playback import HoldEnded
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import VolumeChangedEvent
from kalinka_server.external_playback import ExternalPlaybackService
from kalinka_server.output_device_router import OutputDeviceRouter
from kalinka_server.playback_arbiter import PlaybackArbiter

from tests.test_renderer_volume_control import _bus, _Device, _prepared, _registry_with


@pytest.fixture
async def rig():
    registry, prefs = _registry_with("rid-a")
    bus = _bus()
    local = _Device(
        SupportedFunction.GET_VOLUME, SupportedFunction.SET_VOLUME, volume=42
    )
    amp = _Device(
        SupportedFunction.GET_VOLUME,
        SupportedFunction.SET_VOLUME,
        SupportedFunction.POWER_OFF,
        volume=27,
    )
    local.set_volume = AsyncMock()
    amp.set_volume = AsyncMock()
    router = OutputDeviceRouter(
        registry,
        prefs,
        lambda: {
            "kalinka-renderer": _prepared(local),
            "musiccast": _prepared(amp),
        },
        bus,
    )
    arbiter = PlaybackArbiter(Mock())
    queue = Mock(
        control=PlaybackControl.queue(),
        preempt=AsyncMock(),
        snapshot=lambda: PlaybackState(),
    )
    arbiter.set_queue(queue)
    service = ExternalPlaybackService("roon", arbiter, registry.active_id, router)
    listener = Mock(
        on_revoked=AsyncMock(), on_command=AsyncMock(), set_volume=AsyncMock()
    )
    hold = await service.acquire("Roon", listener, volume_control=listener)
    try:
        yield hold, listener, router, bus, arbiter, queue, prefs, local, amp, service
    finally:
        await arbiter.shutdown()
        await hold.release()
        bus.close()
        await registry.shutdown()


async def test_volume_tracks_hold_through_pause_and_returns_on_stop(rig):
    hold, listener, router, bus, arbiter, _, _, local, _, _ = rig
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=30))
    assert (await router.current().get_volume()).current_volume == 30
    assert bus.get_snapshot().volume.current_volume == 30
    hold.report(PlaybackState(state=PlayerStateEnum.PAUSED))
    await router.current().set_volume(25)
    listener.set_volume.assert_awaited_once_with(25)
    local.set_volume.assert_not_called()
    # Only confirmed values reach the UI. A renderer update cannot overwrite it.
    router.emitter_for("kalinka-renderer").dispatch(
        VolumeChangedEvent(volume=DeviceVolume(current_volume=92))
    )
    assert bus.get_snapshot().volume.current_volume == 30
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=25))
    assert bus.get_snapshot().volume.current_volume == 25
    old_device = router.current()
    await arbiter.take_back()
    assert router.current() is local
    assert bus.get_snapshot().volume.current_volume == 42
    with pytest.raises(HoldEnded):
        hold.report_volume(DeviceVolume(current_volume=99))
    with pytest.raises(HoldEnded):
        await old_device.set_volume(99)


async def test_amp_delegation_keeps_precedence_and_power(rig):
    hold, listener, router, bus, _, _, prefs, _, amp, _ = rig
    prefs.set_volume_control("rid-a", "musiccast")
    await router.resync()
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=100))
    assert router.current() is amp
    assert SupportedFunction.POWER_OFF in router.current().supported_functions()
    assert bus.get_snapshot().volume.current_volume == 27
    await router.current().set_volume(20)
    amp.set_volume.assert_awaited_once_with(20)
    listener.set_volume.assert_not_called()
    prefs.set_volume_control("rid-a", None)
    await router.resync()
    assert bus.get_snapshot().volume.current_volume == 100


async def test_fixed_volume_does_not_fall_back_to_renderer(rig):
    hold, _, router, bus, _, _, _, local, _, _ = rig
    hold.report_volume(DeviceVolume(supported=False))
    assert not (await router.current().get_volume()).supported
    assert not bus.get_snapshot().volume.supported
    with pytest.raises(NotImplementedError):
        await router.current().set_volume(25)
    local.set_volume.assert_not_called()


async def test_a_volume_outside_the_reported_range_is_refused(rig):
    hold, listener, router, _, _, _, _, _, _, _ = rig
    hold.report_volume(DeviceVolume(max_volume=30, current_volume=10))
    with pytest.raises(ValueError):
        await router.current().set_volume(31)
    listener.set_volume.assert_not_called()


async def test_takeover_ends_pending_volume_and_old_release_cannot_clear_new_hold(
    rig,
):
    hold, listener, router, _, arbiter, queue, _, local, _, service = rig
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=30))
    entered = asyncio.Event()

    async def slow_volume(value):
        entered.set()
        await asyncio.Event().wait()

    listener.set_volume.side_effect = slow_volume
    request = asyncio.create_task(router.current().set_volume(20))
    await entered.wait()
    await arbiter.acquire(queue)
    assert router.current() is local
    with pytest.raises(HoldEnded):
        await request
    other = await service.acquire("Roon", listener, volume_control=listener)
    other.report_volume(DeviceVolume(max_volume=100, current_volume=10))
    await hold.release()
    assert (await router.current().get_volume()).current_volume == 10
    await other.release()


async def test_old_plugin_without_volume_keeps_existing_routing(rig):
    hold, listener, router, _, _, _, _, local, _, service = rig
    await hold.release()
    other = await service.acquire("Old plugin", listener)
    assert router.current() is local
    with pytest.raises(ValueError, match="volume_control"):
        other.report_volume(DeviceVolume())
    await other.release()


async def test_slow_volume_resync_cannot_block_audio_stop(rig):
    _, listener, router, _, arbiter, _, _, _, _, _ = rig

    async def stalled():
        await asyncio.Event().wait()

    router.resync = stalled
    await asyncio.wait_for(arbiter.take_back(), 0.5)
    listener.on_revoked.assert_awaited_once()


async def test_a_plugin_ignoring_cancellation_cannot_block_audio_stop(rig):
    hold, listener, router, _, arbiter, _, _, _, _, _ = rig
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=30))
    entered, done = asyncio.Event(), asyncio.Event()

    async def stubborn_volume(value):
        entered.set()
        while not done.is_set():
            try:
                await done.wait()
            except asyncio.CancelledError:
                pass

    listener.set_volume.side_effect = stubborn_volume
    request = asyncio.create_task(router.current().set_volume(20))
    await entered.wait()
    stop = asyncio.create_task(arbiter.take_back())
    finished, _ = await asyncio.wait({stop}, timeout=0.5)
    done.set()
    await asyncio.gather(stop, request, return_exceptions=True)
    assert stop in finished
    listener.on_revoked.assert_awaited_once()


async def test_a_backlog_of_changes_forwards_only_the_latest(rig):
    hold, listener, router, _, _, _, _, _, _, _ = rig
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=30))
    entered, reply = asyncio.Event(), asyncio.Event()

    async def slow_volume(value):
        entered.set()
        await reply.wait()

    listener.set_volume.side_effect = slow_volume
    device = router.current()
    first = asyncio.create_task(device.set_volume(31))
    await entered.wait()
    backlog = [asyncio.create_task(device.set_volume(v)) for v in range(32, 41)]
    await asyncio.sleep(0)
    reply.set()
    await asyncio.gather(first, *backlog)
    assert [c.args for c in listener.set_volume.await_args_list] == [(31,), (40,)]


async def test_a_change_without_a_backlog_is_forwarded(rig):
    hold, listener, router, _, _, _, _, _, _, _ = rig
    hold.report_volume(DeviceVolume(max_volume=100, current_volume=30))
    device = router.current()
    await device.set_volume(31)
    await device.set_volume(32)
    assert [c.args for c in listener.set_volume.await_args_list] == [(31,), (32,)]
