"""Core owns the inactivity clock; Supervisor alone powers off the host."""

import asyncio
import time
from collections.abc import Callable

from fastapi import FastAPI, Response
from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.api import PlayQueueController, ReplayEvent
from kalinka_plugin_sdk.datamodel import PlayerStateEnum
from kalinka_plugin_sdk.events import (
    PlaybackStateChangedEvent,
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)

from .config_model import ServerConfig
from .server_identity import get_server_id

TIMER_PATHS = frozenset(
    {
        "base_config.server.box_power_off_enabled",
        "base_config.server.box_power_off_minutes",
    }
)


class BoxPowerOffTimer:
    """An event-loop-owned clock, reset by activity, settings changes and restart."""

    def __init__(self, config: ServerConfig, now: Callable[[], float] = time.monotonic):
        self.config = config
        self._now = now
        self._idle_since: float | None = None
        self._settings: tuple[bool, int] | None = None
        self.ready = False

    def reset(self) -> None:
        self._idle_since = None

    def observe(self, state: PlayerStateEnum | None) -> None:
        settings = (
            self.config.box_power_off_enabled,
            self.config.box_power_off_minutes,
        )
        if settings != self._settings:
            self.reset()
            self._settings = settings
        if (
            not settings[0]
            or self.config.demo_mode
            or state not in (None, PlayerStateEnum.STOPPED, PlayerStateEnum.PAUSED)
        ):
            self.reset()
        elif self._idle_since is None:
            self._idle_since = self._now()

    def due(self, state: PlayerStateEnum | None) -> bool:
        self.observe(state)
        return (
            self.ready
            and self._idle_since is not None
            and self._now() - self._idle_since >= self.config.box_power_off_minutes * 60
        )

    async def run(
        self, eventbus: EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent]
    ) -> None:
        try:
            async with eventbus.stream(
                [PlayQueueEventType.PlaybackStateChanged]
            ) as stream:
                async for event in stream:
                    if isinstance(event, ReplayEvent):
                        self.observe(event.state.playback_state.state)
                        self.ready = True
                    elif isinstance(event, PlaybackStateChangedEvent):
                        self.observe(event.state.state)
        finally:
            self.ready = False
            self.reset()


def register_power_off_timer_route(
    app: FastAPI, timer: BoxPowerOffTimer, playqueue: PlayQueueController
) -> None:
    """Expose a fresh decision to the local Supervisor, including Core's identity."""

    @app.get("/server/power-off-timer")
    async def power_off_timer(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            playback = await asyncio.wait_for(playqueue.get_playback_state(), timeout=2)
        except Exception:
            timer.reset()
            return {"server_id": get_server_id(), "due": False}
        return {"server_id": get_server_id(), "due": timer.due(playback.state)}
