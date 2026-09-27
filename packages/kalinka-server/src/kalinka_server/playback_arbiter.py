"""Who drives the output: the play queue, or an input plugin that took it over.

The play queue owns the output unless an input plugin playing outside it — a
Connect receiver — has acquired it. The queue takes it back the moment the
listener plays from it, and the plugin is told. Exactly one owner at a time,
and only the owner's playback state reaches clients: the queue keeps
reporting on its list while a plugin plays, and none of it may cover what is
actually audible.

Lock discipline: nothing running on the play queue's serial lane waits on the
lock, because the lock's holder may be waiting on that lane to preempt the
queue. The queue claims through a lock-free fast path while it owns the
output, and an owner is committed only once its predecessor has let go.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Protocol, cast

from kalinka_plugin_sdk.api import EventEmitter
from kalinka_plugin_sdk.datamodel import PlaybackControl, PlaybackState
from kalinka_plugin_sdk.direct_playback import RevokeReason, TransportRequest
from kalinka_plugin_sdk.events import (
    PlaybackControlChangedEvent,
    PlaybackStateChangedEvent,
)

logger = logging.getLogger(__name__.split(".")[-1])


class OutputOwner(Protocol):
    """Something that plays through the output.

    @note preempt() is awaited under the arbiter's lock and must not call
        back into the arbiter.
    """

    @property
    def control(self) -> PlaybackControl:
        """How clients are told who this owner is."""
        ...

    @property
    def renderer_id(self) -> Optional[str]:
        """The renderer it holds a session on, if any."""
        ...

    async def preempt(self, reason: RevokeReason) -> None:
        """Stop and let the output go, because someone else takes it."""
        ...

    def snapshot(self) -> Optional[PlaybackState]:
        """Its playback as clients should see it now."""
        ...


class PluginOwner(OutputOwner, Protocol):
    """An input plugin playing outside the queue."""

    def handle_command(self, request: TransportRequest) -> bool:
        """Pass a client's transport control on; False once the hold has ended."""
        ...

    async def move_to(self, renderer_id: str, commit: Callable[[], None]) -> None:
        """Carry on playing on another renderer, from where playback has reached.

        ``commit`` makes the choice of renderer stick; it is called once the
        target is claimed, or at once when there is nothing to move. A target
        that refuses raises, leaving playback and the choice as they were.
        """
        ...


class QueueOwner(OutputOwner, Protocol):
    """The play queue: the owner every hold falls back to."""

    async def release_renderer(self, renderer_id: str) -> bool:
        """Give this renderer up if the queue plays on it."""
        ...


class PlaybackArbiter:
    """Hands the output between the play queue and plugin holders.

    Also the one writer of PlaybackStateChangedEvent: owners report through
    report(), and only the current owner's reports are dispatched.
    """

    def __init__(self, emitter: EventEmitter):
        self._emitter = emitter
        self._queue: Optional[QueueOwner] = None
        self._owner: Optional[OutputOwner] = None
        self._lock = asyncio.Lock()
        self._control = PlaybackControl.queue()
        self._last_state = PlaybackState()

    def set_queue(self, queue: QueueOwner) -> None:
        """The default owner, which every hold falls back to."""
        self._queue = queue
        if self._owner is None:
            self._owner = queue

    @property
    def control(self) -> PlaybackControl:
        """Who drives the output, as clients are told."""
        return self._control

    @property
    def held_by_plugin(self) -> bool:
        return self._owner is not self._queue

    def owns(self, owner: OutputOwner) -> bool:
        return self._owner is owner

    async def acquire(self, owner: OutputOwner) -> None:
        """Make ``owner`` the output's owner, preempting the current one."""
        if self._owner is owner:
            return
        async with self._lock:
            previous = self._require_owner()
            if previous is owner:
                return
            reason = (
                RevokeReason.QUEUE_PLAY
                if owner is self._queue
                else RevokeReason.OTHER_HOLDER
            )
            await _preempt(previous, reason)
            self._switch_to(owner)

    async def release(self, owner: OutputOwner) -> None:
        """``owner`` let the output go; the play queue has it again."""
        if self._owner is not owner or owner is self._queue:
            return
        async with self._lock:
            if self._owner is owner:
                self._switch_to(self._require_queue())

    async def take_back(self) -> None:
        """A client stopped the plugin's playback: the queue has the output again."""
        await self._revoke_plugin(RevokeReason.TAKEN_BACK)

    async def vacate(self, renderer_id: str) -> None:
        """Stop whatever plays on this renderer, for a one-off use of it.

        For the speaker test, which may address any renderer, not only the
        one playback goes to.
        """
        async with self._lock:
            owner = self._require_owner()
            if owner is not self._queue and owner.renderer_id == renderer_id:
                await _preempt(owner, RevokeReason.OTHER_HOLDER)
                self._switch_to(self._require_queue())
        await self._require_queue().release_renderer(renderer_id)

    async def shutdown(self) -> None:
        await self._revoke_plugin(RevokeReason.SHUTDOWN)

    def report(self, owner: OutputOwner, state: PlaybackState) -> None:
        """Publish an owner's playback; dropped unless it owns the output."""
        if owner is self._owner:
            self._publish(state)

    def forward(self, request: TransportRequest) -> bool:
        """Route a transport control to a plugin holder. False when none holds."""
        owner = self._plugin_owner()
        return owner is not None and owner.handle_command(request)

    async def move_holder(self, renderer_id: str, commit: Callable[[], None]) -> bool:
        """Take a plugin's playback to another renderer. False when none holds.

        Lock-free, for the queue's lane: a preempt that lands meanwhile ends
        the hold, which the move then leaves be.
        """
        owner = self._plugin_owner()
        if owner is None:
            return False
        await owner.move_to(renderer_id, commit)
        return True

    def current_state(self) -> PlaybackState:
        """The last playback state published to clients."""
        return self._last_state

    async def _revoke_plugin(self, reason: RevokeReason) -> None:
        async with self._lock:
            owner = self._require_owner()
            if owner is self._queue:
                return
            await _preempt(owner, reason)
            self._switch_to(self._require_queue())

    def _switch_to(self, owner: OutputOwner) -> None:
        self._owner = owner
        control = owner.control
        if control != self._control:
            self._control = control
            logger.info(
                "Output now driven by %s",
                f"{control.title} ({control.plugin_id})"
                if control.is_exclusive
                else "the play queue",
            )
            self._emitter.dispatch(PlaybackControlChangedEvent(control=control))
        state = owner.snapshot()
        if state is None:
            state = self._require_queue().snapshot()
        if state is not None:
            self._publish(state)

    def _publish(self, state: PlaybackState) -> None:
        self._last_state = state
        self._emitter.dispatch(PlaybackStateChangedEvent(state=state))

    def _plugin_owner(self) -> Optional[PluginOwner]:
        owner = self._owner
        if owner is None or owner is self._queue:
            return None
        return cast(PluginOwner, owner)

    def _require_queue(self) -> QueueOwner:
        if self._queue is None:
            raise RuntimeError("the play queue has not joined the arbiter")
        return self._queue

    def _require_owner(self) -> OutputOwner:
        if self._owner is None:
            raise RuntimeError("the play queue has not joined the arbiter")
        return self._owner


async def _preempt(owner: OutputOwner, reason: RevokeReason) -> None:
    # The output changes hands whatever the old owner's teardown does.
    try:
        await owner.preempt(reason)
    except Exception:
        logger.exception("Preempting the output's owner failed")
