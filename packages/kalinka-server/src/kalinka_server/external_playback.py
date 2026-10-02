"""Playback ownership for engines (such as Roon Bridge) outside the renderer."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from kalinka_plugin_sdk.datamodel import PlaybackControl, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import HoldEnded, RevokeReason
from kalinka_plugin_sdk.external_playback import ExternalPlaybackListener

from .module_timeout import PLUGIN_CALL_TIMEOUT_S
from .playback_arbiter import PlaybackArbiter

logger = logging.getLogger(__name__)


class ExternalSession:
    def __init__(self, plugin_id, title, listener, arbiter, renderer_id):
        self.control = PlaybackControl.exclusive(plugin_id, title)
        self.renderer_id = renderer_id
        self._listener = listener
        self._arbiter = arbiter
        self._active = True
        self._state = PlaybackState(state=PlayerStateEnum.STOPPED)
        self._commands: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._worker = asyncio.create_task(self._deliver_commands())
        self._teardown = None

    @property
    def active(self):
        return self._active

    def snapshot(self):
        return self._state

    def report(self, state):
        if not self.active:
            raise HoldEnded("external playback has ended")
        self._state = state.model_copy(
            deep=True, update={"index": None, "timestamp_ns": time.monotonic_ns()}
        )
        self._arbiter.report(self, self._state)

    async def release(self):
        await self._end(None)
        await self._arbiter.release(self)

    async def preempt(self, reason):
        await self._end(reason)

    async def _end(self, reason):
        if self._teardown is None:
            self._active = False
            self._teardown = asyncio.create_task(
                self._stop(reason, asyncio.current_task() is self._worker)
            )
        await asyncio.shield(self._teardown)

    async def _stop(self, reason, from_command):
        if not from_command:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if reason is not None:
            # Unlike renderer playback, this callback *is* the audio stop.
            # Wait before allowing the next owner to open the physical DAC.
            try:
                await asyncio.wait_for(
                    self._listener.on_revoked(reason), PLUGIN_CALL_TIMEOUT_S
                )
            except Exception:
                logger.exception(
                    "%s failed to stop external audio", self.control.plugin_id
                )

    def handle_command(self, request):
        if not self.active:
            return False
        try:
            self._commands.put_nowait(request)
        except asyncio.QueueFull:
            logger.warning("Dropping excessive external playback commands")
        return True

    async def _deliver_commands(self):
        while self.active:
            request = await self._commands.get()
            try:
                await asyncio.wait_for(
                    self._listener.on_command(request), PLUGIN_CALL_TIMEOUT_S
                )
            except Exception:
                logger.exception("External playback command failed")

    async def move_to(self, renderer_id, commit):
        await self.preempt(RevokeReason.OUTPUT_LOST)
        await self._arbiter.release(self)
        commit()


class ExternalPlaybackService:
    def __init__(
        self,
        plugin_id: str,
        arbiter: PlaybackArbiter,
        renderer_id: Callable[[], str | None] = lambda: None,
    ):
        self._plugin_id = plugin_id
        self._arbiter = arbiter
        self._renderer_id = renderer_id
        self._lock = asyncio.Lock()

    async def acquire(self, title: str, listener: ExternalPlaybackListener):
        async with self._lock:
            session = ExternalSession(
                self._plugin_id, title, listener, self._arbiter, self._renderer_id()
            )
            try:
                await self._arbiter.acquire(session)
            except BaseException:
                await session.release()
                raise
            return session
