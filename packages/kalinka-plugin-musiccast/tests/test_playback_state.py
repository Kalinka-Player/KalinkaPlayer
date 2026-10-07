"""The plugin acts on playback changing state, not on each report of a state.

A PLAYING state is published again whenever anything else about playback
moves — a Spotify Connect stream's control points, its format becoming known
— and each of those once logged "Playback started" and re-ran the ReplayGain
adjustment.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalinka_plugin_sdk.api import ReplayEvent
from kalinka_plugin_sdk.datamodel import (
    Album,
    EntityId,
    EntityType,
    PlaybackMode,
    PlaybackState,
    PlayerStateEnum,
    Track,
)
from kalinka_plugin_sdk.events import PlaybackStateChangedEvent, PlayQueueState
from kalinka_plugin_sdk.ext_device import DeviceVolume

from kalinka_plugin_musiccast.config_model import KalinkaPluginMusiccastConfig
from kalinka_plugin_musiccast.musiccast import KalinkaPluginMusiccastDevice

PLAYING = PlayerStateEnum.PLAYING
PAUSED = PlayerStateEnum.PAUSED
STOPPED = PlayerStateEnum.STOPPED


class _Queue:
    """Delivers a fixed run of play-queue events, then ends the stream."""

    def __init__(self, events):
        self._events = events

    @asynccontextmanager
    async def stream(self, event_types):
        async def events():
            for event in self._events:
                yield event

        yield events()


def _track(track_id: str, gain: float | None = None) -> Track:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="spotify")
    return Track(
        id=entity,
        title=f"song {track_id}",
        duration=240,
        album=Album(id=entity, title="album"),
        replaygain_gain=gain,
    )


def _changed(state: PlayerStateEnum, track: Track | None = None):
    return PlaybackStateChangedEvent(
        state=PlaybackState(state=state, current_track=track)
    )


@pytest.fixture
def device():
    config = KalinkaPluginMusiccastConfig(
        connected_input="netusb", zone_name="main", auto_volume_correction=True
    )
    dev = KalinkaPluginMusiccastDevice(config, MagicMock(), MagicMock())
    dev.volume = DeviceVolume(max_volume=60, current_volume=30, volume_gain=0)

    async def set_volume(volume: int) -> None:
        dev.volume.current_volume = volume

    dev.set_volume = AsyncMock(side_effect=set_volume)
    return dev


async def _listen(device, *events) -> None:
    device.listener = _Queue(list(events))
    await device._playback_state_listener()


def _started(caplog) -> int:
    return caplog.messages.count("Playback started, calling _on_playing")


@pytest.mark.unit
async def test_a_steady_stream_starts_once(device, caplog):
    song = _track("1")
    with caplog.at_level(logging.INFO, logger="musiccast"):
        await _listen(device, *[_changed(PLAYING, song) for _ in range(30)])

    assert _started(caplog) == 1


@pytest.mark.unit
async def test_resuming_after_a_pause_starts_again(device, caplog):
    song = _track("1")
    with caplog.at_level(logging.INFO, logger="musiccast"):
        await _listen(
            device,
            _changed(PLAYING, song),
            _changed(PAUSED, song),
            _changed(PLAYING, song),
            _changed(PLAYING, song),
        )

    assert _started(caplog) == 2


@pytest.mark.unit
async def test_a_stop_is_acted_on_once(device):
    device._on_stopped = AsyncMock()

    await _listen(
        device,
        _changed(PLAYING, _track("1")),
        _changed(STOPPED),
        _changed(STOPPED),
    )

    device._on_stopped.assert_awaited_once()


@pytest.mark.unit
async def test_each_track_still_gets_its_own_replaygain(device, caplog):
    """A gapless change never leaves PLAYING, so the track is what moves."""
    first, second = _track("1", gain=-3.0), _track("2", gain=-6.0)

    with caplog.at_level(logging.INFO, logger="musiccast"):
        await _listen(
            device,
            _changed(PLAYING, first),
            _changed(PLAYING, first),
            _changed(PLAYING, second),
            _changed(PLAYING, second),
        )

    assert [c.args[0] for c in device.set_volume.await_args_list] == [24, 18]
    assert _started(caplog) == 1


@pytest.mark.unit
async def test_a_reconnect_mid_track_lets_replaygain_apply_again(device):
    """get_ready() reads the level back without the gain it carried, and a
    gain set while the device was away was never sent at all."""
    song = _track("1", gain=-3.0)
    device._get_status = AsyncMock(
        return_value={"max_volume": 60, "volume": 30, "power": "on", "input": "netusb"}
    )

    await _listen(device, _changed(PLAYING, song))
    await device.get_ready()
    await _listen(device, _changed(PLAYING, song))

    assert [c.args[0] for c in device.set_volume.await_args_list] == [24, 24]


@pytest.mark.unit
async def test_an_untagged_gapless_track_restores_uncorrected_volume(device):
    await _listen(
        device,
        _changed(PLAYING, _track("tagged", gain=-3.0)),
        _changed(PLAYING, _track("untagged")),
        _changed(PLAYING, _track("untagged")),
    )
    assert [c.args[0] for c in device.set_volume.await_args_list] == [24, 30]
    assert device.volume.volume_gain == 0


@pytest.mark.unit
async def test_the_replayed_state_is_not_a_transition(device):
    device._on_playing = AsyncMock()
    replay = ReplayEvent(
        state_type="PlayQueueState",
        state=PlayQueueState(
            playback_state=PlaybackState(state=PLAYING, current_track=_track("1")),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        ),
        server_time_ns=0,
        seq=0,
    )

    await _listen(
        device,
        replay,
        _changed(PLAYING, _track("1")),
        _changed(PLAYING, _track("1")),
    )

    device._on_playing.assert_awaited_once()
