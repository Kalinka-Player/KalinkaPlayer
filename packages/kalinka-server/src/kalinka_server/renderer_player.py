"""The play queue's player, backed by a session on the active renderer.

Presents the surface the embedded AudioPlayer had — append / remove /
clear_all / pause / resume / stop / seek / get_state, plus monitor() — so the
play queue does not know playback moved out of process. Commands stay
fire-and-forget: they return at once and their effect comes back as state,
delivered through the monitor in the same StreamState shape the native player
produced.

A renderer can also go away mid-track and come back able to play, which no
in-process player did: that arrives as on_interrupted() rather than a stop,
because only the queue can resolve the track again.

The session is opened on demand: the first append() claims the registry's
active renderer (the client-selected one when connected, otherwise the first
connected). It is released when playback stops — stop() closes it
immediately, a finished or errored queue releases it after a short grace, and a
paused one after a longer timeout. A session the renderer side ends
(restart, another Core, renderer lost) surfaces as a STOPPED state with a
message; the next play opens a fresh one.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable, Optional

from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackSource

from .config_model import KalinkaConfig
from .content_urls import content_url
from .renderer_registry import RendererRegistry, RendererUnavailable
from .renderer_sessions import (
    CloseReason,
    PlaybackSession,
    RendererBusy,
    SessionNotActive,
    SessionOpenFailed,
    SessionPool,
    SessionState,
)
from .renderer_state import StateChange
from .stream_state import (
    AudioGraphNodeState,
    StateMonitor,
    StreamError,
    StreamErrorSource,
    StreamState,
    from_snapshot,
    to_stream_id,
)
from .tasks import detach

logger = logging.getLogger(__name__.split(".")[-1])

# How long a finished / stopped / errored session is held before release, so an
# auto-advance that is still resolving the next URL does not reopen every time.
IDLE_RELEASE_TIMEOUT_S = 15.0

# How long a paused session is held before release; a config setting later.
PAUSE_RELEASE_TIMEOUT_S = 600.0


# Reasons the play queue need not hear about: the first is our own close, the
# second is the whole server going down.
_QUIET_CLOSE_REASONS = {CloseReason.CLOSED_BY_SERVER, CloseReason.SHUTDOWN}

# The renderer is there to play again as soon as it is asked.
_RESUMABLE_CLOSE_REASONS = {CloseReason.RENDERER_RESTARTED}

# Playing as a listener would have it: a stall reports PREPARING.
_PLAYING_STATES = (AudioGraphNodeState.PREPARING, AudioGraphNodeState.STREAMING)


class RendererPlayer:
    def __init__(
        self,
        config: KalinkaConfig,
        registry: RendererRegistry,
        sessions: SessionPool,
        monitor: StateMonitor,
        *,
        before_claim: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self._config = config
        # Awaited with the renderer id before every claim, so whoever owns
        # the output can hand it over first.
        self._before_claim = before_claim
        self._registry = registry
        self._pool = sessions
        # Passed in, not owned: the queue listens to one monitor for its whole
        # life, while a player lasts only as long as the renderer it drives.
        self._monitor = monitor
        self._last_state = StreamState(
            state=AudioGraphNodeState.STOPPED, timestamp=time.monotonic_ns()
        )
        self._session: Optional[PlaybackSession] = None
        self._interrupted: Optional[Callable[[int], Any]] = None
        self._session_lost: Optional[Callable[[Optional[CloseReason]], Any]] = None
        # What each live stream was addressed as — minted per renderer, here.
        self._stream_uris: dict[int, str] = {}
        # Commands are applied strictly in call order by one sender task.
        self._ops: asyncio.Queue = asyncio.Queue()
        self._sender_task: Optional[asyncio.Task] = None
        self._release_task: Optional[asyncio.Task] = None
        # Bumped on every published state; an armed release fires only if the
        # player is still in the state that armed it.
        self._epoch = 0
        # Direct sequential playback uses real renderer feedback for pacing.
        self.publish_snapshots = False

    # ------------------------------------------------------------------
    # The AudioPlayer surface

    def get_state(self) -> StreamState:
        return self._last_state

    async def request_snapshot(self) -> None:
        if self._session is not None:
            await self._session.request_snapshot()

    def stream_uri(self, stream_id: Optional[int]) -> Optional[str]:
        """Where the renderer is fetching this stream from, while it holds it."""
        if stream_id is None:
            return None
        return self._stream_uris.get(stream_id)

    def on_interrupted(self, callback: Callable[[int], Any]) -> None:
        """Called with the position when playback was cut short but the
        renderer can play again. An async callback runs on its own."""
        self._interrupted = callback

    def on_session_lost(
        self, callback: Callable[[Optional[CloseReason]], Any]
    ) -> None:
        """Called when the session goes without this player's owner asking:
        with the renderer's close reason, or None after the idle timeout. Not
        called for a close resumed through on_interrupted()."""
        self._session_lost = callback

    def append(
        self, stream_id: int, source: TrackSource, start_offset_ms: int = 0
    ) -> None:
        """start_offset_ms starts the source partway in, without a seek."""
        self._submit("append", stream_id, source, start_offset_ms)

    def remove(self, stream_id: int) -> None:
        self._submit("remove", stream_id)

    def clear_all(self) -> None:
        self._submit("clear_all")

    def pause(self) -> None:
        self._submit("pause")

    def resume(self) -> None:
        self._submit("resume")

    def stop(self) -> None:
        self._submit("stop")

    def seek(self, position_ms: int) -> int:
        self._submit("seek", position_ms)
        # Echo the target; the reached position comes back as state.
        return position_ms

    async def shutdown(self) -> None:
        """Drop the session and stop the sender; the monitor is the caller's."""
        for task in (self._release_task, self._sender_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._release_task = None
        self._sender_task = None
        session, self._session = self._session, None
        if session is not None and session.state is not SessionState.CLOSED:
            await session.close(CloseReason.CLOSED_BY_SERVER)

    # ------------------------------------------------------------------
    # Command delivery

    def _submit(self, op: str, *args) -> None:
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.get_running_loop().create_task(self._run())
        self._ops.put_nowait((op, args))

    async def _run(self) -> None:
        while True:
            op, args = await self._ops.get()
            try:
                await self._dispatch(op, *args)
            except (
                RendererUnavailable,
                RendererBusy,
                SessionOpenFailed,
                SessionNotActive,
                asyncio.TimeoutError,
            ) as exc:
                logger.warning("Renderer command %s failed: %s", op, exc)
                self._publish(
                    StreamState(
                        state=AudioGraphNodeState.ERROR,
                        timestamp=time.monotonic_ns(),
                        error=StreamError(
                            source=StreamErrorSource.AUDIO_OUTPUT,
                            message=str(exc) or "renderer unavailable",
                        ),
                    )
                )
            except Exception:
                logger.exception("Renderer command %s failed", op)

    async def _dispatch(self, op: str, *args) -> None:
        if op == "append":
            stream_id, source, start_offset_ms = args
            session = await self._ensure_session()
            uri = self._resolve_uri(source, session.renderer_id)
            await session.enqueue_source(
                uri,
                mime_type=source.format or "",
                source_token=str(stream_id),
                start_offset_ms=start_offset_ms,
            )
            self._stream_uris[stream_id] = uri
            return
        if op == "stop":
            await self._release(synthesize_stopped=True)
            return

        session = self._session
        if session is None:
            logger.debug("Dropping %s: no renderer session", op)
            return
        if op == "remove":
            self._stream_uris.pop(args[0], None)
            await session.remove_source(str(args[0]))
        elif op == "clear_all":
            self._stream_uris.clear()
            await session.clear_queue()
        elif op == "pause":
            await session.pause()
        elif op == "resume":
            await session.resume()
        elif op == "seek":
            await session.seek(args[0])

    def _resolve_uri(self, source: TrackSource, renderer_id: str) -> str:
        """The URI this renderer should fetch the source from.

        A ModuleAsset is served by us, so it is addressed at the very address
        this renderer dialed to register — the one address it is known to
        reach. A DirectUrl is the module's own and goes out untouched.
        """
        if isinstance(source.source, DirectUrl):
            return source.source.url

        record = self._registry.get(renderer_id)
        if record is None or record.server_addr is None:
            raise RendererUnavailable(
                f"renderer {renderer_id} has no server address to serve content on"
            )
        return content_url(
            record.server_addr, source.source.module, source.source.asset_id
        )

    # ------------------------------------------------------------------
    # Session lifecycle

    @property
    def renderer_id(self) -> Optional[str]:
        return self._session.renderer_id if self._session is not None else None

    async def open(self, renderer_id: str, *, announce: bool = True) -> None:
        """Claim a renderer now rather than at the first command, raising if it
        refuses. ``announce=False`` withholds the claim from the pool's open
        hooks until :meth:`announce`, for one that may still be abandoned."""
        await self._claim(renderer_id, announce=announce)

    async def announce(self) -> None:
        """Tell the pool's open hooks this session is the output now."""
        if self._session is not None:
            await self._pool.announce(self._session)

    async def release(self) -> None:
        """Stop playback and drop the session, reporting STOPPED."""
        await self._release(synthesize_stopped=True)

    async def _ensure_session(self) -> PlaybackSession:
        if self._session is not None and self._session.state is not SessionState.CLOSED:
            return self._session
        renderer_id = self._registry.active_id()
        if renderer_id is None:
            raise RendererUnavailable("no renderer is connected")
        return await self._claim(renderer_id, announce=True)

    async def _claim(self, renderer_id: str, *, announce: bool) -> PlaybackSession:
        if self._before_claim is not None:
            await self._before_claim(renderer_id)
        session = await self._pool.open(renderer_id, announce=announce)
        session.on_state(self._on_session_state)
        session.on_closed(self._on_session_closed)
        session.on_suspended(self._on_session_suspended)
        self._session = session
        self._registry.session_claimed(renderer_id)
        logger.info("Claimed renderer %s for playback", renderer_id)
        return session

    async def _release(self, synthesize_stopped: bool) -> None:
        self._cancel_release()
        self._stream_uris.clear()
        session, self._session = self._session, None
        if session is None:
            return
        self._registry.session_released(session.renderer_id)
        await session.close(CloseReason.CLOSED_BY_SERVER)
        if synthesize_stopped:
            self._publish(
                StreamState(
                    state=AudioGraphNodeState.STOPPED, timestamp=time.monotonic_ns()
                )
            )

    def _on_session_closed(self, session: PlaybackSession, reason: CloseReason) -> None:
        if session is not self._session:
            return  # a close we initiated; already detached
        self._session = None
        self._registry.session_released(session.renderer_id)
        self._cancel_release()
        if reason in _QUIET_CLOSE_REASONS:
            return
        if (
            reason in _RESUMABLE_CLOSE_REASONS
            and self._interrupted is not None
            and self._last_state.state in _PLAYING_STATES
        ):
            position = self._last_state.position_at(time.monotonic_ns())
            logger.info(
                "Renderer session ended (%s) while playing; resuming at %d ms",
                reason.value,
                position,
            )
            result = self._interrupted(position)
            if inspect.isawaitable(result):
                detach(result)
            return
        logger.warning("Renderer session ended: %s", reason.value)
        self._publish(
            StreamState(
                state=AudioGraphNodeState.STOPPED,
                timestamp=time.monotonic_ns(),
                error=StreamError(
                    source=StreamErrorSource.AUDIO_OUTPUT,
                    message=f"renderer session ended ({reason.value})",
                ),
            )
        )
        self._notify_session_lost(reason)

    # ------------------------------------------------------------------
    # State delivery

    def _on_session_state(
        self, session: PlaybackSession, change: StateChange, snapshot: dict
    ) -> None:
        if session is not self._session:
            return
        if change is StateChange.SOURCE:
            self._publish(
                StreamState(
                    state=AudioGraphNodeState.SOURCE_CHANGED,
                    timestamp=time.monotonic_ns(),
                    stream_id=to_stream_id(snapshot.get("source_token")),
                )
            )
            return
        translated = from_snapshot(snapshot)
        if translated is None:
            return
        if change is StateChange.SNAPSHOT:
            # At open this restates what we already report and publishing it
            # would be noise. On resume it is the only evidence of what the
            # renderer did while it was unreachable — those transitions were
            # dropped for want of anywhere to send them.
            if translated.state is self._last_state.state:
                self._record(translated)
                if self.publish_snapshots and translated.state in _PLAYING_STATES:
                    self._monitor.push(translated)
            else:
                self._publish(translated)
            return
        if change is StateChange.PLAYBACK:
            self._publish(translated)

    def _on_session_suspended(self, session: PlaybackSession) -> None:
        """The link dropped mid-playback. The renderer is most likely playing
        on, and the session is held for its return, but we can no longer say
        what it is doing — so report a stall until the snapshot on resume
        settles it, rather than a position that keeps advancing on faith."""
        if session is not self._session:
            return
        if self._last_state.state not in (
            AudioGraphNodeState.PREPARING,
            AudioGraphNodeState.STREAMING,
        ):
            return
        self._publish(
            replace(
                self._last_state,
                state=AudioGraphNodeState.PREPARING,
                # Frozen, or the stall rewinds to the last state change.
                position=self._last_state.position_at(time.monotonic_ns()),
                timestamp=time.monotonic_ns(),
            )
        )

    def _record(self, state: StreamState) -> None:
        """Take the state as current without telling the queue, which already
        believes this. The epoch is deliberately left alone: any release armed
        for the state we are in is still the right one, and bumping it would
        defuse that timer without arming another."""
        self._last_state = state

    def _publish(self, state: StreamState) -> None:
        self._record(state)
        self._epoch += 1
        self._arm_release(state)
        self._monitor.push(state)

    # ------------------------------------------------------------------
    # Idle release

    def _arm_release(self, state: StreamState) -> None:
        self._cancel_release()
        if self._session is None:
            return
        if state.state is AudioGraphNodeState.PAUSED:
            self._release_task = asyncio.get_running_loop().create_task(
                self._release_after(
                    PAUSE_RELEASE_TIMEOUT_S, self._epoch, synthesize=True
                )
            )
        elif state.state in (
            AudioGraphNodeState.FINISHED,
            AudioGraphNodeState.STOPPED,
            AudioGraphNodeState.ERROR,
        ):
            self._release_task = asyncio.get_running_loop().create_task(
                self._release_after(IDLE_RELEASE_TIMEOUT_S, self._epoch, synthesize=False)
            )

    async def _release_after(self, delay: float, epoch: int, synthesize: bool) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if epoch != self._epoch or self._session is None:
            return
        # This task is done waiting; _release must not cancel it mid-close.
        self._release_task = None
        logger.info("Releasing idle renderer session")
        await self._release(synthesize_stopped=synthesize)
        self._notify_session_lost(None)

    def _notify_session_lost(self, reason: Optional[CloseReason]) -> None:
        if self._session_lost is None:
            return
        result = self._session_lost(reason)
        if inspect.isawaitable(result):
            detach(result)

    def _cancel_release(self) -> None:
        if self._release_task is not None:
            self._release_task.cancel()
            self._release_task = None
