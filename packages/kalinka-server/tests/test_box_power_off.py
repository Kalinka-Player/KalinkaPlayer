"""No system actions: Core only reports whether its inactivity clock is due."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.events import PlaybackStateChangedEvent, PlayQueueState
import pytest

from kalinka_server.box_power_off import (
    BoxPowerOffTimer,
    register_power_off_timer_route,
)
from kalinka_server.config_model import ServerConfig


@pytest.fixture
def clock():
    clock = SimpleNamespace(now=0.0)
    clock.config = ServerConfig(box_power_off_enabled=True, box_power_off_minutes=2)
    clock.timer = BoxPowerOffTimer(clock.config, lambda: clock.now)
    clock.timer.ready = True
    return clock


@pytest.mark.parametrize(
    "state", [None, PlayerStateEnum.STOPPED, PlayerStateEnum.PAUSED]
)
def test_idle_for_the_whole_duration(clock, state):
    assert not clock.timer.due(state)
    clock.now = 119.9
    assert not clock.timer.due(state)
    clock.now = 120
    assert clock.timer.due(state)


def test_paused_to_stopped_and_repeated_updates_preserve_idle_time(clock):
    clock.timer.observe(PlayerStateEnum.PAUSED)
    clock.now = 60
    clock.timer.observe(PlayerStateEnum.PAUSED)
    clock.now = 90
    clock.timer.observe(PlayerStateEnum.STOPPED)
    clock.now = 120
    assert clock.timer.due(PlayerStateEnum.STOPPED)


@pytest.mark.parametrize(
    "state", [PlayerStateEnum.PLAYING, PlayerStateEnum.BUFFERING, PlayerStateEnum.ERROR]
)
def test_activity_or_uncertain_playback_requires_a_new_idle_period(clock, state):
    clock.timer.observe(PlayerStateEnum.STOPPED)
    clock.now = 119
    assert not clock.timer.due(state)
    clock.now = 120
    assert not clock.timer.due(PlayerStateEnum.PAUSED)
    clock.now = 239
    assert not clock.timer.due(PlayerStateEnum.PAUSED)
    clock.now = 240
    assert clock.timer.due(PlayerStateEnum.PAUSED)


def test_disable_and_reenable_start_over(clock):
    clock.timer.observe(None)
    clock.now = 120
    clock.config.box_power_off_enabled = False
    assert not clock.timer.due(None)
    clock.config.box_power_off_enabled = True
    assert not clock.timer.due(None)
    clock.now = 240
    assert clock.timer.due(None)


def test_changing_duration_starts_over(clock):
    clock.timer.observe(None)
    clock.now = 119
    clock.config.box_power_off_minutes = 1
    assert not clock.timer.due(None)
    clock.now = 179
    assert clock.timer.due(None)


def test_defaults_demo_and_unready_never_authorize_shutdown(clock):
    assert not ServerConfig().box_power_off_enabled
    clock.timer.observe(None)
    clock.now = 10000
    clock.timer.ready = False
    assert not clock.timer.due(None)
    clock.timer.ready = True
    clock.config.demo_mode = True
    assert not clock.timer.due(None)


def test_process_restart_starts_a_full_new_period(clock):
    clock.timer.observe(None)
    clock.now = 120
    restarted = BoxPowerOffTimer(clock.config, lambda: clock.now)
    restarted.ready = True
    assert not restarted.due(None)
    clock.now = 240
    assert restarted.due(None)


def test_route_rechecks_playback_and_fails_closed(clock, monkeypatch):
    monkeypatch.setattr("kalinka_server.box_power_off.get_server_id", lambda: "box-1")
    playqueue = SimpleNamespace(
        get_playback_state=AsyncMock(return_value=PlaybackState())
    )
    app = FastAPI()
    register_power_off_timer_route(app, clock.timer, playqueue)
    with TestClient(app) as client:
        assert client.get("/server/power-off-timer").json()["due"] is False
        clock.now = 120
        response = client.get("/server/power-off-timer")
        assert response.json() == {"server_id": "box-1", "due": True}
        assert response.headers["Cache-Control"] == "no-store"
        playqueue.get_playback_state.return_value = PlaybackState(
            state=PlayerStateEnum.PLAYING
        )
        assert not client.get("/server/power-off-timer").json()["due"]
        playqueue.get_playback_state.return_value = PlaybackState()
        assert not client.get("/server/power-off-timer").json()["due"]
        clock.now = 240
        playqueue.get_playback_state.side_effect = RuntimeError("unavailable")
        assert not client.get("/server/power-off-timer").json()["due"]
        playqueue.get_playback_state.side_effect = None
        assert not client.get("/server/power-off-timer").json()["due"]


async def test_event_stream_catches_activity_between_supervisor_polls(clock):
    bus = EventBus(
        PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    timer = BoxPowerOffTimer(clock.config, lambda: clock.now)
    task = asyncio.create_task(timer.run(bus))

    async def wait_until(predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.001)

    try:
        await wait_until(lambda: timer.ready)
        clock.now = 119
        bus.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(state=PlayerStateEnum.PLAYING)
            )
        )
        await wait_until(lambda: timer._idle_since is None)
        clock.now = 120
        bus.dispatch(
            PlaybackStateChangedEvent(state=PlaybackState(state=PlayerStateEnum.PAUSED))
        )
        await wait_until(lambda: timer._idle_since == 120)
        assert not timer.due(PlayerStateEnum.PAUSED)
        clock.now = 240
        assert timer.due(PlayerStateEnum.PAUSED)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        bus.close()
    assert not timer.due(None)
