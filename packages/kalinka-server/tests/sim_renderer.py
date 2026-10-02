"""A renderer on the far end of a fake wire.

Plays the part the kalinka-renderer binary plays in production: registers with
the registry, accepts one session at a time, and answers playback commands with
the same protobuf state messages the real NativePlayer emits — delivered
through the real SessionPool, so tests exercise the whole server-side path
(pool routing, snapshot merging, RendererPlayer translation, play queue).

The simulated graph mirrors the native one's observable behaviour: enqueueing
onto an idle player starts it (SourceChanged -> preparing -> playing, the
format riding on every state once the decoder knows it), removing the current
source hands over to the next queued one or finishes, clearing finishes.
Playback never advances on its own — a test moves it with finish_current().
"""

from __future__ import annotations

from typing import Optional

from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.renderer_state import StateChange

SAMPLE_RATE = 44100
CHANNELS = 2
BITS_PER_SAMPLE = 16
# Long enough that the play queue's prefetch timer never fires mid-test.
DURATION_MS = 300000

_NOW_UNIX_MS = 1700000000000


class SimRenderer:
    RENDERER_ID = "sim-renderer"

    def __init__(
        self,
        registry: RendererRegistry,
        pool: SessionPool,
        renderer_id: Optional[str] = None,
    ):
        if renderer_id is not None:
            self.RENDERER_ID = renderer_id
        self.registry = registry
        self.pool = pool
        self.session_id: Optional[str] = None
        self.current: Optional[str] = None  # source token
        # The graph rests in FINISHED after a source runs out, not STOPPED.
        self.finished = False
        # The graph carries a format into later states; a new source has none.
        self.format_known = False
        # Clear it to play a dropped link: the graph runs on, but state has
        # nowhere to go and the renderer drops it rather than queueing it.
        self.linked = True
        self.queued: list[str] = []
        self.start_offsets: dict[str, int] = {}
        self.position_ms = 0
        self.commands: list[pb.Command] = []
        self.volume = 40
        self.volume_supported = True
        self.volume_backend = pb.VOLUME_BACKEND_HARDWARE
        self.config_updates: list[dict] = []
        self.volume_policies: list[bool] = []
        # Set accept=False to play a renderer another Core already holds.
        self.accept = True
        self.busy_owner = "another-core"
        # A RendererConfigService to answer config updates through; without it
        # updates are recorded but never acknowledged (the caller times out).
        self.configs = None
        self._message_id = 0
        # The address this renderer reached the server on, as the ws handler
        # records it; content URLs are minted against it.
        self.server_addr: Optional[tuple[str, int]] = ("192.0.2.10", 8000)

    def connect(self, compatible: bool = True) -> None:
        self.registry.register(
            renderer_id=self.RENDERER_ID,
            instance_id="sim-instance",
            friendly_name="Sim Renderer",
            software_version="0.0.0",
            kind="native",
            platform={},
            session=self,
            compatible=compatible,
            server_addr=self.server_addr,
        )

    # ------------------------------------------------------------------
    # The ws-session surface the pool drives

    async def send_session_open(
        self,
        session_id: str,
        force_fixed_output: bool = False,
    ) -> None:
        self.volume_policies.append(force_fixed_output)
        if not self.accept:
            self.pool.handle_open_result(
                self.RENDERER_ID,
                session_id=session_id,
                accepted=False,
                busy=True,
                detail="another playback session is running",
                owner_server_id=self.busy_owner,
            )
            return
        self.session_id = session_id
        self.pool.handle_open_result(
            self.RENDERER_ID,
            session_id=session_id,
            accepted=True,
            busy=False,
            detail="",
            owner_server_id="",
        )
        self._send(StateChange.SNAPSHOT, self._snapshot())

    async def send_session_close(self, session_id: str, reason) -> None:
        if session_id == self.session_id:
            self.session_id = None
            self.current = None
            self.queued.clear()
            self.position_ms = 0
        self.pool.handle_closed(
            self.RENDERER_ID, session_id=session_id, renderer_error=False, detail=""
        )

    async def send_command(self, session_id: str, command: pb.Command) -> None:
        copied = pb.Command()
        copied.CopyFrom(command)
        self.commands.append(copied)
        op = command.WhichOneof("op")
        if op == "enqueue_source":
            self._enqueue(command.enqueue_source.source)
        elif op == "set_source":
            displaced = list(self.queued)
            if self.current is not None:
                displaced.append(self.current)
            self._enqueue(command.set_source.source)
            for token in displaced:
                self._remove(token)
        elif op == "remove_source":
            self._remove(command.remove_source.source_token)
        elif op == "clear_queue":
            self.queued.clear()
            had_current, self.current = self.current is not None, None
            if had_current:
                self._emit_state(pb.PLAYBACK_STATE_FINISHED, None)
        elif op == "pause":
            if self.current is not None:
                self._emit_state(pb.PLAYBACK_STATE_PAUSED, self.current)
        elif op == "resume":
            if self.current is not None:
                self._emit_state(pb.PLAYBACK_STATE_PLAYING, self.current)
        elif op == "stop":
            self.queued.clear()
            had_current, self.current = self.current is not None, None
            if had_current:
                self._emit_state(pb.PLAYBACK_STATE_STOPPED, None)
        elif op == "seek":
            if self.current is not None:
                # The native pipeline re-buffers on seek.
                self.position_ms = command.seek.position_ms
                self._emit_state(pb.PLAYBACK_STATE_PREPARING, self.current)
                self._emit_state(pb.PLAYBACK_STATE_PLAYING, self.current)
        elif op == "set_volume":
            self.volume = min(command.set_volume.percent, 100)
            changed = pb.VolumeChanged()
            self._fill_volume(changed.volume)
            self._send(StateChange.VOLUME, changed)
        elif op == "request_snapshot":
            self._send(StateChange.SNAPSHOT, self._snapshot())

    def next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def send_config_update(self, message_id: int, changes: dict) -> None:
        self.config_updates.append(dict(changes))
        if self.configs is None:
            return
        result = pb.ConfigResult()
        for path, value in changes.items():
            outcome = result.outcomes.add()
            outcome.path = path
            outcome.applied = True
            outcome.value = str(value)
        self.configs.handle_reply(self.RENDERER_ID, self, message_id, result)

    async def replace(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Test controls

    def finish_current(self) -> None:
        """The current track runs to its end."""
        ended, self.current = self.current, None
        if ended is None:
            return
        if self.queued:
            self._start(self.queued.pop(0))
        else:
            self.finished = True
            self._emit_state(pb.PLAYBACK_STATE_FINISHED, ended)

    def report_position(self, position_ms: int) -> None:
        """The current track has reached position_ms."""
        self.position_ms = position_ms
        if self.current is not None:
            self._emit_state(pb.PLAYBACK_STATE_PLAYING, self.current)

    def fail_current(self, message: str = "http error") -> None:
        """The current track's stream breaks mid-play."""
        failed, self.current = self.current, None
        state = pb.PlaybackStateChanged()
        state.state = pb.PLAYBACK_STATE_ERROR
        state.position_ms = self.position_ms
        state.position_valid = False
        if failed is not None:
            state.source_token = failed
        state.error.source = pb.ERROR_SOURCE_HTTP_STREAM
        state.error.message = message
        state.at_unix_ms = _NOW_UNIX_MS
        self._send(StateChange.PLAYBACK, state)

    # ------------------------------------------------------------------
    # The simulated graph

    def _enqueue(self, source: pb.Source) -> None:
        token = source.source_token
        self.start_offsets[token] = source.start_offset_ms
        if self.current is None:
            self._start(token)
        else:
            self.queued.append(token)

    def _remove(self, token: str) -> None:
        if token == self.current:
            self.current = None
            if self.queued:
                self._start(self.queued.pop(0))
            else:
                self._emit_state(pb.PLAYBACK_STATE_FINISHED, token)
        elif token in self.queued:
            self.queued.remove(token)

    def _start(self, token: str) -> None:
        previous, self.current = self.current, token
        self.finished = False
        # Where the source was told to begin, as the decoder's start offset.
        self.position_ms = self.start_offsets.get(token, 0)
        changed = pb.SourceChanged()
        changed.source_token = token
        if previous is not None:
            changed.previous_source_token = previous
        changed.at_unix_ms = _NOW_UNIX_MS
        self._send(StateChange.SOURCE, changed)
        self.format_known = False
        self._emit_state(pb.PLAYBACK_STATE_PREPARING, token)
        self.format_known = True
        self._emit_state(pb.PLAYBACK_STATE_PLAYING, token)

    def _fill_format(self, out: pb.AudioFormat) -> None:
        out.sample_rate_hz = SAMPLE_RATE
        out.channels = CHANNELS
        out.bits_per_sample = BITS_PER_SAMPLE
        out.sample_format = "S16_LE"

    def _fill_device_info(self, out: pb.DeviceInfo) -> None:
        self._fill_format(out.format)
        out.access = pb.DEVICE_ACCESS_EXCLUSIVE

    def _emit_state(self, state_value, token: Optional[str]) -> None:
        state = pb.PlaybackStateChanged()
        state.state = state_value
        if self.format_known:
            self._fill_format(state.format)
            self._fill_device_info(state.device_info)
            state.duration_ms = DURATION_MS
        state.position_ms = self.position_ms
        state.position_valid = state_value in (
            pb.PLAYBACK_STATE_PLAYING,
            pb.PLAYBACK_STATE_PAUSED,
        )
        if token is not None:
            state.source_token = token
        state.at_unix_ms = _NOW_UNIX_MS
        self._send(StateChange.PLAYBACK, state)

    def _snapshot(self) -> pb.StateSnapshot:
        snapshot = pb.StateSnapshot()
        if self.current is not None:
            snapshot.playback_state = pb.PLAYBACK_STATE_PLAYING
        elif self.finished:
            snapshot.playback_state = pb.PLAYBACK_STATE_FINISHED
        else:
            snapshot.playback_state = pb.PLAYBACK_STATE_STOPPED
        snapshot.position_ms = self.position_ms
        snapshot.position_valid = self.current is not None
        snapshot.captured_at_unix_ms = _NOW_UNIX_MS
        if self.format_known:
            self._fill_format(snapshot.format)
            self._fill_device_info(snapshot.device_info)
            snapshot.duration_ms = DURATION_MS
        self._fill_volume(snapshot.volume)
        return snapshot

    def turn_knob(self, percent: int) -> None:
        """The volume changed on the renderer's host, not by a command."""
        self.volume = percent
        changed = pb.VolumeChanged(external=True)
        self._fill_volume(changed.volume)
        self._send(StateChange.VOLUME, changed)

    def _fill_volume(self, out: pb.VolumeState) -> None:
        out.supported = self.volume_supported
        out.current = self.volume
        out.max = 100
        out.backend = (
            self.volume_backend if self.volume_supported else pb.VOLUME_BACKEND_NONE
        )

    def _send(self, change: StateChange, message) -> None:
        if self.session_id is None or not self.linked:
            return
        self.pool.handle_state(
            self.RENDERER_ID,
            session_id=self.session_id,
            change=change,
            message=message,
        )
