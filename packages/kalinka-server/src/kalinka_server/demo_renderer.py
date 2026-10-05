"""An output that plays nothing, in time.

A demo server has no sound card and no renderer process, yet the queue, the
progress bar and track changes must behave as they do at home. This renderer
lives in the server and is reached through the same registry and session pool
a connected one is: it fetches no audio, and its clock is the wall clock, so a
track ends when its length says it should.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from .renderer_config import RendererConfigService
from .renderer_proto import renderer_pb2 as pb
from .renderer_registry import RendererRegistry
from .renderer_sessions import SessionPool
from .renderer_state import StateChange
from .version import get_version

RENDERER_ID = "demo-output"
_NAME = "Demo output"
_NO_SETTINGS = "The demo output has no settings"
_MAX_VOLUME = 100


class Cancellable(Protocol):
    """A timer's handle: calling it off is all that can be done with it."""

    def cancel(self) -> Any: ...


class Clock(Protocol):
    """Time as the demo output keeps it."""

    def now_ms(self) -> int:
        """Monotonic milliseconds, which positions are measured in."""
        ...

    def unix_ms(self) -> int:
        """Wall-clock milliseconds, which the protocol stamps states with."""
        ...

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Cancellable:
        """Call ``callback`` once, ``delay_s`` from now, on the event loop."""
        ...


class LoopClock:
    """The running event loop's clock."""

    def now_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def unix_ms(self) -> int:
        return int(time.time() * 1000)

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Cancellable:
        return asyncio.get_running_loop().call_later(delay_s, callback)


class DemoRenderer:
    """A renderer that keeps time without making a sound.

    Reports what the native player does — SourceChanged, PREPARING, then
    PLAYING; FINISHED at rest; PREPARING again on a seek — but fetches and
    decodes nothing, so it names no format and no device, only the length. A
    source runs for the length its track declares and then hands over to the
    next queued one, as gapless playback does. A source whose length is
    unknown plays until it is skipped.

    Implements the RendererLink the pool and the config service drive. Every
    call arrives on the server's event loop, and every timer fires there.

    @param duration_ms_of How long the source a token names runs, or None when
        nobody knows. Asked once, when the source is enqueued.
    @param clock A LoopClock unless a test steps time by hand.
    """

    def __init__(
        self,
        registry: RendererRegistry,
        pool: SessionPool,
        configs: RendererConfigService,
        duration_ms_of: Callable[[str], int | None],
        clock: Clock | None = None,
    ):
        self._registry = registry
        self._pool = pool
        self._configs = configs
        self._duration_ms_of = duration_ms_of
        self._clock: Clock = clock if clock is not None else LoopClock()
        self._instance_id = uuid.uuid4().hex
        self._message_id = 0
        self._session_id: str | None = None
        self._current: str | None = None
        self._queued: list[str] = []
        self._sources: dict[str, pb.Source] = {}
        self._durations: dict[str, int | None] = {}
        # Where the current source stood when its clock last started or stopped.
        self._base_ms = 0
        # None while the clock is stopped: paused, or nothing playing.
        self._started_at_ms: int | None = None
        self._finish: Cancellable | None = None
        self._finished = False
        self._volume = 40

    def connect(self) -> None:
        """Register with the registry, as a renderer's Hello does."""
        self._registry.register(
            renderer_id=RENDERER_ID,
            instance_id=self._instance_id,
            friendly_name=_NAME,
            software_version=get_version(),
            kind="native",
            platform={"os": "simulated"},
            session=self,
            compatible=True,
            upgrade_supported=False,
        )

    async def shutdown(self) -> None:
        self._cancel_finish()

    def next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def send_session_open(
        self, session_id: str, force_fixed_output: bool = False
    ) -> None:
        self._reset()
        self._session_id = session_id
        self._pool.handle_open_result(
            RENDERER_ID,
            session_id=session_id,
            accepted=True,
            busy=False,
            detail="",
            owner_server_id="",
        )
        self._send(StateChange.SNAPSHOT, self._snapshot())

    async def send_session_close(self, session_id: str, reason: Any) -> None:
        if session_id == self._session_id:
            self._reset()
            self._session_id = None

    async def send_command(self, session_id: str, command: pb.Command) -> None:
        if session_id != self._session_id:
            return
        op = command.WhichOneof("op")
        if op == "enqueue_source":
            self._enqueue(command.enqueue_source.source)
        elif op == "set_source":
            displaced = list(self._queued)
            if self._current is not None:
                displaced.append(self._current)
            self._enqueue(command.set_source.source)
            for token in displaced:
                self._remove(token)
        elif op == "remove_source":
            self._remove(command.remove_source.source_token)
        elif op == "clear_queue":
            self._drop_queued()
            ended = self._end_current()
            if ended is not None:
                self._emit(pb.PLAYBACK_STATE_FINISHED, None, 0)
        elif op == "stop":
            self._drop_queued()
            ended = self._end_current()
            if ended is not None:
                self._emit(pb.PLAYBACK_STATE_STOPPED, None, 0)
        elif op == "pause":
            self._pause()
        elif op == "resume":
            self._resume()
        elif op == "seek":
            self._seek(command.seek.position_ms)
        elif op == "set_volume":
            self._volume = min(command.set_volume.percent, _MAX_VOLUME)
            changed = pb.VolumeChanged()
            self._fill_volume(changed.volume)
            self._send(StateChange.VOLUME, changed)
        elif op == "request_snapshot":
            self._send(StateChange.SNAPSHOT, self._snapshot())

    async def send_config_request(self, message_id: int) -> None:
        self._configs.handle_reply(
            RENDERER_ID, self, message_id, pb.ConfigSnapshot(config_version="demo")
        )

    async def send_config_update(
        self, message_id: int, changes: dict[str, str]
    ) -> None:
        result = pb.ConfigResult(config_version="demo")
        for path in changes:
            result.outcomes.add(path=path, applied=False, error=_NO_SETTINGS)
        self._configs.handle_reply(RENDERER_ID, self, message_id, result)

    async def send_upgrade(self, message_id: int, target_version: str) -> None:
        pass

    async def replace(self) -> None:
        pass

    def _position_ms(self) -> int:
        position = self._base_ms
        if self._started_at_ms is not None:
            position += self._clock.now_ms() - self._started_at_ms
        duration = self._durations.get(self._current or "")
        return min(position, duration) if duration else position

    def _enqueue(self, source: pb.Source) -> None:
        token = source.source_token
        self._sources[token] = pb.Source()
        self._sources[token].CopyFrom(source)
        self._durations[token] = self._duration_ms_of(token)
        if self._current is None:
            self._start(token)
        else:
            self._queued.append(token)

    def _remove(self, token: str) -> None:
        if token == self._current:
            self._end_current()
            if self._queued:
                self._start(self._queued.pop(0))
            else:
                self._emit(pb.PLAYBACK_STATE_FINISHED, token, 0)
        elif token in self._queued:
            self._queued.remove(token)
            self._forget(token)

    def _start(self, token: str) -> None:
        previous, self._current = self._current, token
        if previous is not None:
            self._forget(previous)
        self._finished = False
        self._base_ms = self._sources[token].start_offset_ms
        self._started_at_ms = self._clock.now_ms()
        changed = pb.SourceChanged(source_token=token, at_unix_ms=self._clock.unix_ms())
        if previous is not None:
            changed.previous_source_token = previous
        self._send(StateChange.SOURCE, changed)
        self._emit(pb.PLAYBACK_STATE_PREPARING, token, self._base_ms)
        self._emit(pb.PLAYBACK_STATE_PLAYING, token, self._base_ms)
        self._arm_finish()

    def _pause(self) -> None:
        if self._current is None:
            return
        self._base_ms = self._position_ms()
        self._started_at_ms = None
        self._cancel_finish()
        self._emit(pb.PLAYBACK_STATE_PAUSED, self._current, self._base_ms)

    def _resume(self) -> None:
        if self._current is None:
            return
        if self._started_at_ms is None:
            self._started_at_ms = self._clock.now_ms()
        self._emit(pb.PLAYBACK_STATE_PLAYING, self._current, self._position_ms())
        self._arm_finish()

    def _seek(self, position_ms: int) -> None:
        if self._current is None:
            return
        playing = self._started_at_ms is not None
        self._base_ms = position_ms
        self._started_at_ms = self._clock.now_ms() if playing else None
        position = self._position_ms()
        self._emit(pb.PLAYBACK_STATE_PREPARING, self._current, position)
        if playing:
            self._emit(pb.PLAYBACK_STATE_PLAYING, self._current, position)
            self._arm_finish()
        else:
            self._emit(pb.PLAYBACK_STATE_PAUSED, self._current, position)

    def _arm_finish(self) -> None:
        self._cancel_finish()
        if self._current is None or self._started_at_ms is None:
            return
        duration = self._durations.get(self._current)
        if not duration:
            return
        remaining_ms = max(0, duration - self._position_ms())
        self._finish = self._clock.call_later(remaining_ms / 1000, self._on_finished)

    def _on_finished(self) -> None:
        self._finish = None
        ended = self._current
        if ended is None:
            return
        if self._queued:
            self._start(self._queued.pop(0))
            return
        position = self._position_ms()
        self._current = None
        self._started_at_ms = None
        self._finished = True
        self._emit(pb.PLAYBACK_STATE_FINISHED, ended, position)
        self._forget(ended)

    def _end_current(self) -> str | None:
        """Take the current source off the graph; the token it had, if any."""
        ended, self._current = self._current, None
        self._started_at_ms = None
        self._base_ms = 0
        self._cancel_finish()
        if ended is not None:
            self._forget(ended)
        return ended

    def _drop_queued(self) -> None:
        for token in self._queued:
            self._forget(token)
        self._queued.clear()

    def _forget(self, token: str) -> None:
        self._sources.pop(token, None)
        self._durations.pop(token, None)

    def _reset(self) -> None:
        self._drop_queued()
        self._end_current()
        self._finished = False

    def _cancel_finish(self) -> None:
        if self._finish is not None:
            self._finish.cancel()
            self._finish = None

    def _emit(self, state: int, token: str | None, position_ms: int) -> None:
        message = pb.PlaybackStateChanged(
            state=state,
            position_ms=position_ms,
            position_valid=state
            in (pb.PLAYBACK_STATE_PLAYING, pb.PLAYBACK_STATE_PAUSED),
            at_unix_ms=self._clock.unix_ms(),
        )
        if token is not None:
            message.source_token = token
            duration = self._durations.get(token)
            if duration:
                message.duration_ms = duration
        self._send(StateChange.PLAYBACK, message)

    def _snapshot(self) -> pb.StateSnapshot:
        snapshot = pb.StateSnapshot(captured_at_unix_ms=self._clock.unix_ms())
        if self._current is not None:
            snapshot.playback_state = (
                pb.PLAYBACK_STATE_PLAYING
                if self._started_at_ms is not None
                else pb.PLAYBACK_STATE_PAUSED
            )
            snapshot.current_source.CopyFrom(self._sources[self._current])
            snapshot.position_ms = self._position_ms()
            snapshot.position_valid = True
            duration = self._durations.get(self._current)
            if duration:
                snapshot.duration_ms = duration
        elif self._finished:
            snapshot.playback_state = pb.PLAYBACK_STATE_FINISHED
        else:
            snapshot.playback_state = pb.PLAYBACK_STATE_STOPPED
        snapshot.queued_source_tokens.extend(self._queued)
        self._fill_volume(snapshot.volume)
        return snapshot

    def _fill_volume(self, out: pb.VolumeState) -> None:
        out.supported = True
        out.current = self._volume
        out.max = _MAX_VOLUME
        out.backend = pb.VOLUME_BACKEND_SOFTWARE

    def _send(self, change: StateChange, message: Any) -> None:
        if self._session_id is None:
            return
        self._pool.handle_state(
            RENDERER_ID, session_id=self._session_id, change=change, message=message
        )
