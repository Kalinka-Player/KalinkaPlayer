"""Playback ownership for engines (such as Roon Bridge) outside the renderer."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from kalinka_plugin_sdk.datamodel import PlaybackControl, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import HoldEnded, RevokeReason, TransportRequest
from kalinka_plugin_sdk.external_playback import ExternalPlaybackListener

from .module_timeout import PLUGIN_CALL_TIMEOUT_S
from .playback_arbiter import PlaybackArbiter

logger = logging.getLogger(__name__)


class ExternalSession:
    def __init__(
        self,
        plugin_id: str,
        title: str,
        listener: ExternalPlaybackListener,
        arbiter: PlaybackArbiter,
        renderer_id: str | None,
    ) -> None:
        self.control: PlaybackControl = PlaybackControl.exclusive(plugin_id, title)
        self.renderer_id: str | None = renderer_id
        self._listener: ExternalPlaybackListener = listener
        self._arbiter: PlaybackArbiter = arbiter
        self._active: bool = True
        self._state: PlaybackState = PlaybackState(state=PlayerStateEnum.STOPPED)
        self._commands: asyncio.Queue[TransportRequest] = asyncio.Queue(maxsize=32)
        self._worker: asyncio.Task[None] = asyncio.create_task(self._deliver_commands())
        self._teardown: asyncio.Task[None] | None = None
        self._release_task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self._active

    def snapshot(self) -> PlaybackState:
        return self._state

    def report(self, state: PlaybackState) -> None:
        if not self.active:
            raise HoldEnded("external playback has ended")
        self._state = state.model_copy(
            deep=True, update={"index": None, "timestamp_ns": time.monotonic_ns()}
        )
        self._arbiter.report(self, self._state)

    async def release(self) -> None:
        await self._release(None)

    async def preempt(self, reason: RevokeReason) -> None:
        # Arbiter handovers own the switch and hold its lock. Never release
        # through the arbiter here, or teardown would wait on that same lock.
        await asyncio.shield(self._begin_end(reason))

    def _begin_end(self, reason: RevokeReason | None) -> asyncio.Task[None]:
        if self._teardown is None:
            self._active = False
            self._teardown = asyncio.create_task(
                self._stop(reason, asyncio.current_task() is self._worker)
            )
        return self._teardown

    async def _release(self, reason: RevokeReason | None) -> None:
        if self._release_task is None:
            # Capture whether this is on_command before starting a new task.
            teardown = self._begin_end(reason)
            self._release_task = asyncio.create_task(self._finish_release(teardown))
        # Retain the full cleanup, including arbitration, if the caller is
        # cancelled. Later release() calls join the same task.
        await asyncio.shield(self._release_task)

    async def _finish_release(self, teardown: asyncio.Task[None]) -> None:
        try:
            await teardown
        finally:
            await self._arbiter.release(self)

    async def _stop(self, reason: RevokeReason | None, from_command: bool) -> None:
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

    def handle_command(self, request: TransportRequest) -> bool:
        if not self.active:
            return False
        try:
            self._commands.put_nowait(request)
        except asyncio.QueueFull:
            logger.warning("Dropping excessive external playback commands")
        return True

    async def _deliver_commands(self) -> None:
        while self.active:
            request = await self._commands.get()
            try:
                # Keep the callback on this task on Python 3.11 too, so a
                # plugin releasing from on_command never cancels itself.
                async with asyncio.timeout(PLUGIN_CALL_TIMEOUT_S):
                    await self._listener.on_command(request)
            except Exception:
                logger.exception("External playback command failed")

    async def move_to(self, renderer_id: str, commit: Callable[[], None]) -> None:
        await self._release(RevokeReason.OUTPUT_LOST)
        commit()


class ExternalPlaybackService:
    def __init__(
        self,
        plugin_id: str,
        arbiter: PlaybackArbiter,
        renderer_id: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._plugin_id: str = plugin_id
        self._arbiter: PlaybackArbiter = arbiter
        self._renderer_id: Callable[[], str | None] = renderer_id
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(
        self, title: str, listener: ExternalPlaybackListener
    ) -> ExternalSession:
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
