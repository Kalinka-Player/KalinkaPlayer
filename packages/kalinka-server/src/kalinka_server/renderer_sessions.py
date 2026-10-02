"""Playback sessions: a Core's exclusive claim on a renderer.

The Core mints the session id; both sides keep it in memory only, so a crash on
either side ends the session. A dropped link only suspends it — the renderer
keeps playing and the session is rebound when it returns. Whatever the two
sides then disagree about is settled by reconcile() at the next Hello:

    renderer reports        Core holds         outcome
    ----------------------------------------------------------------------
    our session S           S                  resume (rebind)
    our session S           nothing            SessionClose(STALE) to renderer
    another Core's session  -                  not ours; ignored
    nothing                 session T          T closed, RENDERER_RESTARTED

Reopening after a close is the owner's decision, never the pool's.

An open session is also the handle for driving the renderer: it carries the
playback commands (the AudioPlayer surface, one method per command) and the
state the renderer reports back. Settings are not here — they belong to the
renderer rather than to whoever is playing through it (renderer_config).
Commands are not acknowledged — what one did shows up in the state that
follows, failure included. The single exception is a
command the renderer refuses outright because it names a session it is not
running; that comes back as a rejection and closes the session, which was
provably not there.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from . import renderer_state
from .renderer_link import RendererLink
from .renderer_proto import renderer_pb2 as pb
from .renderer_registry import RendererRegistry
from .renderer_state import StateChange
from .tasks import detach

logger = logging.getLogger(__name__.split(".")[-1])

# Network timeout, nothing more: the renderer is on the LAN, and neither a
# session-open round trip nor a socket write is waiting on anything it does.
DEFAULT_TIMEOUT_S = 3.0


class SessionState(str, Enum):
    OPENING = "opening"
    ACTIVE = "active"
    SUSPENDED = "suspended"  # renderer offline; session kept for its return
    CLOSED = "closed"


class CloseReason(str, Enum):
    CLOSED_BY_SERVER = "closed_by_server"
    STALE = "stale"
    SHUTDOWN = "shutdown"
    RENDERER_RESTARTED = "renderer_restarted"
    RENDERER_SHUTDOWN = "renderer_shutdown"  # renderer said goodbye
    RENDERER_LOST = "renderer_lost"  # renderer went silent and was reaped
    RENDERER_ERROR = "renderer_error"
    REJECTED_BY_RENDERER = "rejected_by_renderer"  # it is not running this session
    OPEN_FAILED = "open_failed"


# The rest describe a renderer that already lost the session; telling it would
# be addressed to a session it no longer has.
_WIRE_CLOSE_REASONS = {
    CloseReason.CLOSED_BY_SERVER,
    CloseReason.STALE,
    CloseReason.SHUTDOWN,
}


class RendererBusy(Exception):
    def __init__(self, message: str, owner_server_id: str = ""):
        super().__init__(message)
        self.owner_server_id = owner_server_id


class SessionOpenFailed(Exception):
    """The renderer refused the session for a reason other than busy."""


class SessionNotActive(Exception):
    """The session is closed, or its renderer is offline right now.

    A suspended session raises this too, and starts working again by itself
    once the renderer reconnects and the session is resumed.
    """


@dataclass
class _OpenOutcome:
    accepted: bool
    busy: bool
    detail: str
    owner_server_id: str


@dataclass(frozen=True)
class SessionVolumePolicy:
    force_fixed_output: bool = False


def _fill_source(
    source, uri: str, mime_type: str, source_token: str, start_offset_ms: int
) -> None:
    source.uri = uri
    source.mime_type = mime_type
    source.source_token = source_token
    source.start_offset_ms = start_offset_ms


def _fanout(callbacks, args: tuple, what: str) -> None:
    """Notify listeners; one that raises must not stop the rest, and one that
    is async runs on its own."""
    for callback in callbacks:
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                detach(result)
        except Exception:
            logger.exception("Session %s callback failed", what)


class PlaybackSession:
    def __init__(
        self,
        pool: "SessionPool",
        session_id: str,
        renderer_id: str,
        ws_session: RendererLink,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.session_id = session_id
        self.renderer_id = renderer_id
        self.state = SessionState.OPENING
        self.opened_at = time.time()
        self.close_reason: Optional[CloseReason] = None
        # What the renderer last reported, snapshots and changes merged.
        self.snapshot: dict = renderer_state.empty_state()
        # The refused command that ended the session, if that is how it ended.
        self.rejection: Optional[dict] = None
        self._pool = pool
        self._ws: Optional[RendererLink] = ws_session
        self._open_future: Optional[asyncio.Future] = None
        self._callbacks: list[Callable] = []
        self._state_callbacks: list[Callable] = []
        self._suspend_callbacks: list[Callable] = []
        self._timeout_s = timeout_s

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def on_closed(
        self, callback: Callable[["PlaybackSession", CloseReason], Any]
    ) -> None:
        if self.state is SessionState.CLOSED:
            reason = self.close_reason or CloseReason.CLOSED_BY_SERVER
            _fanout([callback], (self, reason), "close")
            return
        self._callbacks.append(callback)

    def on_state(
        self, callback: Callable[["PlaybackSession", StateChange, dict], Any]
    ) -> None:
        """Called as callback(session, change, snapshot) on every change."""
        self._state_callbacks.append(callback)

    def on_suspended(self, callback: Callable[["PlaybackSession"], Any]) -> None:
        """Called when the link carrying this session drops and it is kept for
        the renderer's return."""
        self._suspend_callbacks.append(callback)

    async def set_source(
        self,
        uri: str,
        *,
        mime_type: str = "",
        source_token: str = "",
        start_offset_ms: int = 0,
    ) -> None:
        """Replace what is playing — append() plus removal of the old stream."""
        command = pb.Command()
        _fill_source(
            command.set_source.source,
            uri,
            mime_type,
            source_token,
            start_offset_ms,
        )
        await self._send(command)

    async def enqueue_source(
        self,
        uri: str,
        *,
        mime_type: str = "",
        source_token: str = "",
        start_offset_ms: int = 0,
    ) -> None:
        """Prefetch for a gapless switch — append() alongside the current source."""
        command = pb.Command()
        _fill_source(
            command.enqueue_source.source,
            uri,
            mime_type,
            source_token,
            start_offset_ms,
        )
        await self._send(command)

    async def remove_source(self, source_token: str) -> None:
        command = pb.Command()
        command.remove_source.source_token = source_token
        await self._send(command)

    async def clear_queue(self) -> None:
        command = pb.Command()
        command.clear_queue.SetInParent()
        await self._send(command)

    async def pause(self) -> None:
        command = pb.Command()
        command.pause.SetInParent()
        await self._send(command)

    async def resume(self) -> None:
        """The inverse of pause(); starting playback is set_source()."""
        command = pb.Command()
        command.resume.SetInParent()
        await self._send(command)

    async def stop(self) -> None:
        command = pb.Command()
        command.stop.SetInParent()
        await self._send(command)

    async def set_volume(self, percent: int) -> None:
        command = pb.Command()
        command.set_volume.percent = percent
        await self._send(command)

    async def seek(self, position_ms: int) -> None:
        command = pb.Command()
        command.seek.position_ms = position_ms
        await self._send(command)

    async def request_snapshot(self) -> None:
        """Ask for full state; it arrives as a state message, not a return value."""
        command = pb.Command()
        command.request_snapshot.SetInParent()
        await self._send(command)

    async def _send(self, command: pb.Command) -> None:
        ws = self._ws
        if self.state is not SessionState.ACTIVE or ws is None:
            raise SessionNotActive(
                f"session {self.session_id} is {self.state.value}"
            )
        try:
            await asyncio.wait_for(
                ws.send_command(self.session_id, command), self._timeout_s
            )
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            # A socket that died between the state check and the write.
            raise SessionNotActive(
                f"could not reach renderer {self.renderer_id}: {exc}"
            ) from exc

    async def close(self, reason: CloseReason = CloseReason.CLOSED_BY_SERVER) -> None:
        """Idempotent, and never waits on the renderer's acknowledgement."""
        if self.state is SessionState.CLOSED:
            return
        ws = self._ws
        self._finish(reason)
        if ws is not None and reason in _WIRE_CLOSE_REASONS:
            await _send_close(ws, self.session_id, reason)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "renderer_id": self.renderer_id,
            "state": self.state.value,
            "opened_at": self.opened_at,
            "connected": self.connected,
            "snapshot": self.snapshot,
            "rejection": self.rejection,
        }

    def _rebind(self, ws_session: RendererLink) -> None:
        self._ws = ws_session
        self.state = SessionState.ACTIVE
        # It kept playing while we were away, so what we hold may be stale.
        detach(self._refresh_state())

    async def _refresh_state(self) -> None:
        try:
            await self.request_snapshot()
        except Exception as exc:
            logger.debug(
                "Could not refresh state of session %s after resume: %s",
                self.session_id,
                exc,
            )

    def _apply_state(self, change: StateChange, message: Any) -> None:
        self.snapshot = renderer_state.apply(self.snapshot, change, message)
        _fanout(list(self._state_callbacks), (self, change, self.snapshot), "state")

    def _suspend(self) -> None:
        self._ws = None
        self.state = SessionState.SUSPENDED
        _fanout(list(self._suspend_callbacks), (self,), "suspend")

    def _finish(self, reason: CloseReason) -> None:
        if self.state is SessionState.CLOSED:
            return
        self.state = SessionState.CLOSED
        self.close_reason = reason
        self._ws = None
        self._pool._forget(self)
        if self._open_future is not None and not self._open_future.done():
            # A result, not an exception: an exception on a future that open()
            # never gets to await would surface as an asyncio ERROR traceback.
            self._open_future.set_result(
                _OpenOutcome(
                    accepted=False,
                    busy=False,
                    detail=f"session closed while opening ({reason.value})",
                    owner_server_id="",
                )
            )
        callbacks, self._callbacks = self._callbacks, []
        _fanout(callbacks, (self, reason), "close")


async def _send_close(
    ws_session: RendererLink, session_id: str, reason: CloseReason
) -> None:
    try:
        await ws_session.send_session_close(session_id, reason)
    except Exception as exc:
        logger.debug("Could not send SessionClose for %s: %s", session_id, exc)


class SessionPool:
    """At most one open session per renderer, keyed by renderer_id."""

    def __init__(
        self,
        registry: RendererRegistry,
        server_id: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._registry = registry
        self.server_id = server_id
        self._timeout_s = timeout_s
        self._sessions: dict[str, PlaybackSession] = {}
        self._open_hooks: list[Callable] = []
        self._volume_policy: Callable[
            [str], SessionVolumePolicy
        ] = lambda _renderer_id: SessionVolumePolicy()

    def set_volume_policy(
        self, provider: Callable[[str], SessionVolumePolicy]
    ) -> None:
        """Install what decides the volume policy carried on SessionOpen.

        A downstream device mapping temporarily fixes the renderer at unity;
        the renderer restores its own configured mode when the session ends.
        """
        self._volume_policy = provider

    def add_open_hook(
        self, hook: Callable[[PlaybackSession], Any]
    ) -> None:
        """Awaited inside open() once the session is ACTIVE, before open()
        returns — so a hook's work (e.g. a volume push) cannot race whatever
        playback commands the caller sends next. A failing hook is logged,
        never fails the open."""
        self._open_hooks.append(hook)

    def remove_open_hook(self, hook: Callable) -> None:
        if hook in self._open_hooks:
            self._open_hooks.remove(hook)

    def get(self, renderer_id: str) -> Optional[PlaybackSession]:
        return self._sessions.get(renderer_id)

    def list(self) -> list[dict]:
        return [session.to_dict() for session in self._sessions.values()]

    async def open(
        self, renderer_id: str, *, announce: bool = True
    ) -> PlaybackSession:
        ws = self._registry.require_session(renderer_id)
        if renderer_id in self._sessions:
            raise RendererBusy(
                f"renderer {renderer_id} already has a session with this Core",
                owner_server_id=self.server_id,
            )

        session = PlaybackSession(
            self, str(uuid.uuid4()), renderer_id, ws, self._timeout_s
        )
        self._sessions[renderer_id] = session
        session._open_future = asyncio.get_running_loop().create_future()

        volume = self._volume_policy(renderer_id)
        try:
            await ws.send_session_open(
                session.session_id,
                force_fixed_output=volume.force_fixed_output,
            )
            outcome = await asyncio.wait_for(
                session._open_future, self._timeout_s
            )
        except BaseException:
            # BaseException, not Exception: a cancelled caller (client gone,
            # outer timeout, shutdown) would otherwise leave the session stuck
            # in OPENING and the renderer busy forever.
            self._abort_open(session)
            raise

        if not outcome.accepted:
            session._finish(CloseReason.OPEN_FAILED)
            if outcome.busy:
                raise RendererBusy(
                    f"renderer {renderer_id} is in use by another Core",
                    owner_server_id=outcome.owner_server_id,
                )
            raise SessionOpenFailed(outcome.detail or "renderer refused the session")

        session.state = SessionState.ACTIVE
        logger.info("Opened session %s on renderer %s", session.session_id, renderer_id)
        if announce:
            await self.announce(session)
        return session

    async def announce(self, session: PlaybackSession) -> None:
        """Run the open hooks for a session that is the output from now on.

        Separate from open() because switching renderers claims the next one
        before giving up the current one, and a device module must not adopt a
        session that may yet be abandoned."""
        for hook in list(self._open_hooks):
            try:
                result = hook(session)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Session open hook failed")

    def _abort_open(self, session: PlaybackSession, notify: bool = True) -> None:
        """Give up on a session still being opened, releasing the renderer.

        The renderer may have accepted and lost the reply, so it is told to drop
        the session — on whichever connection the session is bound to now, which
        is not necessarily the one open() sent on. Fire-and-forget, because the
        caller may be unwinding a cancellation.
        """
        ws = session._ws
        session._finish(CloseReason.OPEN_FAILED)
        if notify and ws is not None:
            detach(
                _send_close(ws, session.session_id, CloseReason.CLOSED_BY_SERVER)
            )

    async def reconcile(
        self,
        *,
        renderer_id: str,
        reported_session_id: str,
        reported_owner_server_id: str,
        ws_session: RendererLink,
    ) -> None:
        """Settle renderer-reported session state against the pool, at Hello."""
        session = self._sessions.get(renderer_id)
        ours = bool(reported_session_id) and reported_owner_server_id == self.server_id

        if session is not None and session.state is SessionState.OPENING:
            # open() is still waiting for a reply on the connection that just
            # died. Adopting the session here would let that pending open()
            # time out and close it again, so start clean instead; anything the
            # renderer accepted is dropped by the stale branch below.
            logger.info(
                "Renderer %s reconnected while session %s was still opening; "
                "abandoning it",
                renderer_id,
                session.session_id,
            )
            self._abort_open(session, notify=False)
            session = None

        if ours and session is not None and session.session_id == reported_session_id:
            session._rebind(ws_session)
            logger.info(
                "Resumed session %s on renderer %s", reported_session_id, renderer_id
            )
            return

        if ours:
            logger.info(
                "Renderer %s reports session %s which this Core no longer has; "
                "closing it as stale",
                renderer_id,
                reported_session_id,
            )
            await _send_close(ws_session, reported_session_id, CloseReason.STALE)

        if session is not None:
            logger.info(
                "Renderer %s returned without session %s; closing it",
                renderer_id,
                session.session_id,
            )
            session._finish(CloseReason.RENDERER_RESTARTED)

    def handle_open_result(
        self,
        renderer_id: str,
        *,
        session_id: str,
        accepted: bool,
        busy: bool,
        detail: str,
        owner_server_id: str,
    ) -> None:
        session = self._sessions.get(renderer_id)
        if (
            session is None
            or session.session_id != session_id
            or session._open_future is None
            or session._open_future.done()
        ):
            logger.debug(
                "Ignoring open result for unknown session %s from %s",
                session_id,
                renderer_id,
            )
            return
        session._open_future.set_result(
            _OpenOutcome(accepted, busy, detail, owner_server_id)
        )

    def handle_rejection(self, renderer_id: str, *, rejection: Any) -> None:
        """A command named a session the renderer is not running.

        Only reachable when our view and the renderer's have diverged without a
        reconnect to settle it, so the session goes — the renderer has just said
        it does not have one. Reopening is the owner's call, as always.
        """
        session = self._session_for(
            renderer_id, rejection.session_id, "command rejection"
        )
        if session is None:
            return
        command = renderer_state.enum_name(
            pb.ControlKind, rejection.command, "CONTROL_KIND_"
        )
        session.rejection = {
            "session_id": rejection.session_id,
            "command": command,
            "at_unix_ms": rejection.at_unix_ms,
            "detail": rejection.detail,
        }
        logger.warning(
            "Renderer %s refused %s on session %s (%s); closing it",
            renderer_id,
            command,
            session.session_id,
            rejection.detail,
        )
        session._finish(CloseReason.REJECTED_BY_RENDERER)

    def handle_state(
        self,
        renderer_id: str,
        *,
        session_id: str,
        change: StateChange,
        message: Any,
    ) -> None:
        session = self._session_for(renderer_id, session_id, change.value)
        if session is not None:
            session._apply_state(change, message)

    def _session_for(
        self, renderer_id: str, session_id: str, what: str
    ) -> Optional[PlaybackSession]:
        session = self._sessions.get(renderer_id)
        if session is None or session.session_id != session_id:
            logger.debug(
                "Ignoring %s from %s for session %s, which is not ours",
                what,
                renderer_id,
                session_id or "<none>",
            )
            return None
        return session

    def handle_closed(
        self,
        renderer_id: str,
        *,
        session_id: str,
        renderer_error: bool,
        detail: str,
    ) -> None:
        session = self._sessions.get(renderer_id)
        if session is None or session.session_id != session_id:
            return
        if renderer_error:
            logger.warning(
                "Renderer %s ended session %s: %s", renderer_id, session_id, detail
            )
            session._finish(CloseReason.RENDERER_ERROR)

    def suspend(self, renderer_id: str, ws_session: RendererLink) -> None:
        """The connection carrying this session dropped."""
        session = self._sessions.get(renderer_id)
        if session is None or session._ws is not ws_session:
            return  # stale disconnect of a replaced connection
        if session.state is SessionState.OPENING:
            session._finish(CloseReason.OPEN_FAILED)
            return
        session._suspend()
        logger.info(
            "Session %s suspended; renderer %s went offline",
            session.session_id,
            renderer_id,
        )

    def handle_renderer_removed(self, renderer_id: str, clean: bool = False) -> None:
        session = self._sessions.get(renderer_id)
        if session is not None:
            logger.info(
                "Renderer %s is gone; closing session %s",
                renderer_id,
                session.session_id,
            )
            session._finish(
                CloseReason.RENDERER_SHUTDOWN if clean else CloseReason.RENDERER_LOST
            )

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            await session.close(CloseReason.SHUTDOWN)

    def _forget(self, session: PlaybackSession) -> None:
        if self._sessions.get(session.renderer_id) is session:
            del self._sessions[session.renderer_id]
