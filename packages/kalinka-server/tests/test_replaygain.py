import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kalinka_plugin_sdk.datamodel import (
    Album,
    DeviceVolume,
    EntityId,
    EntityType,
    PlaybackState,
    PlayerStateEnum,
    Track,
    VolumeBackend,
)
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_server.renderer_prefs import RendererPreferences
from kalinka_server.renderer_output_device import RendererVolumeDevice
from kalinka_server.replaygain import ReplayGainController


class Device:
    def __init__(
        self, level=50, backend=VolumeBackend.SOFTWARE, mode="auto", curve=None
    ):
        self.volume = DeviceVolume(
            current_volume=level,
            max_volume=100,
            backend=backend,
            control_mode=mode,
            db_values=curve or [],
        )
        self.writes = []

    async def get_volume(self):
        return self.volume.model_copy(deep=True)

    async def set_volume(self, level):
        self.writes.append(level)
        self.volume.current_volume = level

    def supported_functions(self):
        return [SupportedFunction.GET_VOLUME, SupportedFunction.SET_VOLUME]


def state(
    track_id="1", gain=-6, album_gain=-3, status=PlayerStateEnum.PLAYING, index=0
):
    entity = EntityId(id=track_id, source="localfiles", type=EntityType.TRACK)
    return PlaybackState(
        state=status,
        index=index,
        current_track=Track(
            id=entity,
            title=track_id,
            duration=10,
            album=Album(id=entity, title="album"),
            replaygain_gain=gain,
            replaygain_album_gain=album_gain,
        ),
    )


@pytest.fixture
def rig():
    prefs = RendererPreferences()
    prefs.set_replaygain("renderer", "track")
    registry = Mock()
    registry.active_id.return_value = "renderer"
    router = Mock()
    device = Device()
    router.current.return_value = device
    controller = ReplayGainController(registry, prefs, router, None, None)
    return SimpleNamespace(
        controller=controller,
        prefs=prefs,
        registry=registry,
        router=router,
        device=device,
    )


async def test_software_applies_db_on_cubic_scale_and_restores_once(rig, caplog):
    with caplog.at_level(logging.INFO, logger="replaygain"):
        await rig.controller.handle(state())
        for _ in range(10):
            await rig.controller.handle(state())
        await rig.controller.handle(state(status=PlayerStateEnum.PAUSED))
        await rig.controller.handle(state())
        await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
        await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert rig.device.writes == [40, 50]
    assert sum("applied" in m for m in caplog.messages) == 1
    assert sum("restored" in m for m in caplog.messages) == 1


async def test_calibrated_hardware_and_musiccast_use_device_db_steps(rig):
    rig.device.volume.backend = VolumeBackend.HARDWARE
    rig.device.volume.db_values = [i * 0.5 for i in range(101)]
    rig.prefs.set_volume_control("renderer", "musiccast")
    await rig.controller.handle(state())
    await rig.controller.handle(state("2", gain=-3))
    await rig.controller.handle(state("3", gain=None))
    assert rig.device.writes == [38, 44, 50]


async def test_a_card_mixer_without_a_db_scale_is_left_alone(rig, caplog):
    rig.device.volume.backend = VolumeBackend.HARDWARE
    with caplog.at_level(logging.INFO, logger="replaygain"):
        await rig.controller.handle(state())
        await rig.controller.handle(state("2"))
    assert rig.device.writes == []
    assert sum("no dB scale" in m for m in caplog.messages) == 1


async def test_losing_the_db_scale_removes_the_correction(rig):
    await rig.controller.handle(state())
    rig.device.volume.backend = VolumeBackend.HARDWARE
    await rig.controller.handle(state("2"))
    assert rig.device.writes == [40, 50]


async def test_an_external_device_without_a_db_scale_keeps_the_linear_fallback(rig):
    rig.device.volume.backend = VolumeBackend.HARDWARE
    rig.device.volume.control_mode = ""
    rig.prefs.set_volume_control("renderer", "device")
    await rig.controller.handle(state())
    assert rig.device.writes == [25]


async def test_logs_capture_current_volume_when_the_device_returns_a_live_cache(
    rig, caplog
):
    rig.device.volume.backend = VolumeBackend.HARDWARE
    rig.device.volume.db_values = [i * 0.5 for i in range(101)]
    rig.device.get_volume = AsyncMock(return_value=rig.device.volume)
    with caplog.at_level(logging.INFO, logger="replaygain"):
        await rig.controller.handle(state())
        await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert "volume 50 -> 38" in caplog.messages[0]
    assert "volume: 38 -> 50" in caplog.messages[1]


async def test_album_mode_preserves_album_dynamics_and_does_not_use_track_fallback(rig):
    rig.prefs.set_replaygain("renderer", "album")
    await rig.controller.handle(state(gain=-12, album_gain=-6))
    await rig.controller.handle(state("2", gain=3, album_gain=-6))
    await rig.controller.handle(state("3", gain=-6, album_gain=None))
    assert rig.device.writes == [40, 50]


@pytest.mark.parametrize("mode", ["auto", "hardware", "software"])
async def test_every_adjustable_volume_mode_is_eligible(rig, mode):
    rig.device.volume.control_mode = mode
    await rig.controller.handle(state())
    assert rig.device.writes == [40]


async def test_fixed_output_is_eligible_only_through_a_connected_device(rig):
    rig.device.volume.control_mode = "fixed"
    await rig.controller.handle(state())
    assert rig.device.writes == []
    rig.prefs.set_volume_control("renderer", "device")
    await rig.controller.refresh()
    assert rig.device.writes == [40]


async def test_fixed_or_unavailable_volume_is_never_changed(rig):
    rig.device.volume.supported = False
    rig.device.volume.backend = VolumeBackend.NONE
    await rig.controller.handle(state())
    assert rig.device.writes == []


@pytest.mark.parametrize("gain", [None, float("nan"), float("inf")])
async def test_missing_or_invalid_gain_restores_previous_track(rig, gain):
    await rig.controller.handle(state())
    await rig.controller.handle(state("2", gain=gain))
    assert rig.device.writes == [40, 50]


async def test_disabling_mid_track_restores_volume(rig):
    await rig.controller.handle(state())
    rig.prefs.set_replaygain("renderer", "disabled")
    await rig.controller.refresh()
    assert rig.device.writes == [40, 50]


@pytest.mark.parametrize(
    "initial,gain,adjusted", [(98, 6, 100), (2, -120, 0), (0, 12, 0)]
)
async def test_clamps_and_restores_exact_baseline_even_at_limits(
    rig, initial, gain, adjusted
):
    rig.device.volume.current_volume = initial
    await rig.controller.handle(state(gain=gain))
    assert rig.device.volume.current_volume == adjusted
    await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert rig.device.volume.current_volume == initial


async def test_manual_volume_changes_are_preserved_on_restore(rig):
    await rig.controller.handle(state())
    rig.device.volume.current_volume = 48
    await rig.controller.refresh()
    await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert rig.device.writes == [40, 60]


async def test_coarse_hardware_echo_restores_the_exact_baseline(rig):
    rig.device.volume.backend = VolumeBackend.HARDWARE
    rig.device.volume.db_values = [round(i / 5) * 2 for i in range(101)]
    write = rig.device.set_volume

    async def set_volume(level):
        await write(level)
        rig.device.volume.current_volume = round(level / 5) * 5

    rig.device.set_volume = set_volume
    await rig.controller.handle(state())
    assert rig.device.volume.current_volume == 35
    await rig.controller.handle(state("2", gain=-4))
    await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert rig.device.writes == [33, 38, 50]


async def test_manual_mute_stays_muted(rig):
    await rig.controller.handle(state())
    rig.device.volume.current_volume = 0
    await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert rig.device.writes == [40, 0]


async def test_mapping_change_restores_old_device_before_correcting_new(rig):
    await rig.controller.handle(state())
    other = Device(level=70)
    rig.router.current.return_value = other
    await rig.controller.refresh()
    assert rig.device.writes == [40, 50]
    assert other.writes == [56]


async def test_mapping_change_while_paused_restores_old_device_before_resume(rig):
    await rig.controller.handle(state())
    await rig.controller.handle(state(status=PlayerStateEnum.PAUSED))
    other = Device(level=70)
    rig.router.current.return_value = other
    await rig.controller.refresh()
    assert rig.device.writes == [40, 50]
    assert other.writes == []
    await rig.controller.handle(state())
    assert other.writes == [56]


async def test_renderer_switch_restores_the_original_renderer_cache(rig):
    device = RendererVolumeDevice(rig.registry, Mock(), Mock())
    device._volumes = {"renderer": Device().volume, "other": Device(level=70).volume}
    rig.router.current.return_value = device
    await rig.controller.handle(state())
    rig.registry.active_id.return_value = "other"
    await rig.controller.handle(PlaybackState(state=PlayerStateEnum.STOPPED))
    assert device._pending == {"renderer": 50}
    assert (await device.get_volume()).current_volume == 70


async def test_failed_write_does_not_commit_offset_and_can_retry(rig):
    write = rig.device.set_volume
    rig.device.set_volume = AsyncMock(side_effect=ConnectionError("offline"))
    await rig.controller.handle(state())
    assert rig.controller._applied is None
    rig.device.set_volume = write
    await rig.controller.refresh()
    assert rig.device.writes == [40]


async def test_failed_restore_is_retried_without_another_gain(rig):
    await rig.controller.handle(state())
    write = rig.device.set_volume
    rig.device.set_volume = AsyncMock(side_effect=ConnectionError("offline"))
    await rig.controller.handle(state("2", gain=None))
    rig.device.set_volume = write
    await rig.controller.refresh()
    assert rig.device.writes == [40, 50]


async def test_playback_error_restores_volume(rig):
    await rig.controller.handle(state())
    await rig.controller.handle(state(status=PlayerStateEnum.ERROR))
    assert rig.device.writes == [40, 50]


async def test_switching_to_fixed_restores_before_volume_becomes_unwritable(rig):
    await rig.controller.handle(state())
    async with rig.controller.changing_volume_mode("renderer"):
        assert rig.device.volume.current_volume == 50
        rig.device.volume.control_mode = "fixed"
        rig.device.volume.supported = False
    await rig.controller.refresh()
    assert rig.device.writes == [40, 50]


async def test_shutdown_waits_for_in_flight_correction_then_restores(rig):
    started, finish = asyncio.Event(), asyncio.Event()
    write = rig.device.set_volume

    async def set_volume(value):
        if value == 40:
            started.set()
            await finish.wait()
        await write(value)

    rig.device.set_volume = set_volume
    playing = asyncio.create_task(rig.controller.handle(state()))
    rig.controller._tasks = [playing]
    await started.wait()
    shutdown = asyncio.create_task(rig.controller.shutdown())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.wait_for(shutdown, 1)
    assert rig.device.writes == [40, 50]


async def test_real_playback_bus_applies_and_shutdown_restores(rig):
    from kalinka_eventbus import EventBus
    from kalinka_plugin_sdk.datamodel import PlaybackMode
    from kalinka_plugin_sdk.events import PlaybackStateChangedEvent, PlayQueueState
    from kalinka_plugin_sdk.ext_device_events import ExtDeviceState

    playback = EventBus(
        initial_state=PlayQueueState(
            playback_state=PlaybackState(state=PlayerStateEnum.STOPPED),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    volumes = EventBus(
        initial_state=ExtDeviceState(power_on=True, volume=rig.device.volume)
    )
    controller = ReplayGainController(
        rig.registry, rig.prefs, rig.router, playback, volumes
    )
    applied = asyncio.Event()
    write = rig.device.set_volume

    async def set_volume(value):
        await write(value)
        applied.set()

    rig.device.set_volume = set_volume
    try:
        await controller.start()
        await asyncio.sleep(0)
        playback.dispatch(PlaybackStateChangedEvent(state=state()))
        await asyncio.wait_for(applied.wait(), 1)
        assert rig.device.writes == [40]
    finally:
        await controller.shutdown()
        playback.close()
        volumes.close()
    assert rig.device.writes == [40, 50]
