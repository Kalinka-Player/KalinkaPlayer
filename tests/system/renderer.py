"""A renderer on the real wire: the websocket the kalinka-renderer binary uses.

It registers, accepts the one playback session the server offers it, and
records the URI of every source it is told to play — which is how the test
learns the address the server hands out for a track, since that address is
minted per renderer against the one it dialed in on.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Optional

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_ws_handler import PROTOCOL_VERSION

_HANDSHAKE_TIMEOUT_S = 30.0


class FakeRenderer:
    RENDERER_ID = "kalinka-system-test-renderer"

    def __init__(self, base_url: str) -> None:
        self._url = base_url.replace("http://", "ws://", 1) + "/renderer/ws"
        self._ws = None
        self._pump: Optional[threading.Thread] = None
        self._session_id: Optional[str] = None
        self._message_id = 0
        self._current: Optional[str] = None
        self._queued: list[str] = []
        self.server_id: Optional[str] = None
        self.sources: queue.Queue[str] = queue.Queue()

    def __enter__(self) -> "FakeRenderer":
        self._ws = connect(self._url, open_timeout=_HANDSHAKE_TIMEOUT_S)
        self._send(self._hello())
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
        while self.server_id is None:
            env = self._receive(deadline - time.monotonic())
            if env.WhichOneof("payload") == "welcome":
                self.server_id = env.welcome.server_id
        self._pump = threading.Thread(target=self._run, daemon=True)
        self._pump.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._ws.close()
        self._pump.join(timeout=10)

    def next_source(self, timeout: float) -> str:
        """The URI of the next source the server asked this renderer to play."""
        return self.sources.get(timeout=timeout)

    def forget_sources(self) -> None:
        """Drop what has been recorded, so the next wait cannot be answered by
        a source command from an earlier one."""
        while True:
            try:
                self.sources.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        while True:
            try:
                env = self._receive(None)
            except (ConnectionClosed, OSError):
                return
            payload = env.WhichOneof("payload")
            if payload == "session_open":
                self._session_id = env.session_open.session_id
                self._accept_session()
            elif payload == "session_close":
                self._session_id = None
                self._queued.clear()
                self._current = None
            elif payload == "command":
                self._handle(env.command)
            elif payload == "goodbye":
                return

    def _handle(self, command: pb.Command) -> None:
        op = command.WhichOneof("op")
        if op in ("set_source", "enqueue_source"):
            source = getattr(command, op).source
            self.sources.put(source.uri)
            self._enqueue(source.source_token)
        elif op == "remove_source":
            self._remove(command.remove_source.source_token)
        elif op in ("clear_queue", "stop"):
            self._queued.clear()
            self._current = None
        elif op == "request_snapshot":
            self._send(self._snapshot())

    def _enqueue(self, source_token: str) -> None:
        """Enqueueing onto an idle graph starts it; onto a busy one it waits,
        which is what the native renderer does."""
        if self._current is None:
            self._start(source_token)
        else:
            self._queued.append(source_token)

    def _remove(self, source_token: str) -> None:
        if source_token != self._current:
            if source_token in self._queued:
                self._queued.remove(source_token)
            return
        self._current = None
        if self._queued:
            self._start(self._queued.pop(0))

    def _accept_session(self) -> None:
        env = self._envelope()
        env.session_open_result.session_id = self._session_id
        env.session_open_result.accepted = True
        self._send(env)
        self._send(self._snapshot())

    def _start(self, source_token: str) -> None:
        self._current = source_token
        env = self._envelope()
        env.source_changed.source_token = source_token
        env.source_changed.at_unix_ms = _now_ms()
        self._send(env)
        env = self._envelope()
        state = env.playback_state_changed
        state.state = pb.PLAYBACK_STATE_PLAYING
        state.source_token = source_token
        state.position_valid = True
        state.at_unix_ms = _now_ms()
        self._send(env)

    def _snapshot(self) -> pb.Envelope:
        env = self._envelope()
        env.state_snapshot.playback_state = pb.PLAYBACK_STATE_STOPPED
        env.state_snapshot.captured_at_unix_ms = _now_ms()
        env.state_snapshot.volume.supported = False
        env.state_snapshot.volume.backend = pb.VOLUME_BACKEND_NONE
        return env

    def _hello(self) -> pb.Envelope:
        env = self._envelope()
        hello = env.hello
        hello.protocol_versions.min = PROTOCOL_VERSION
        hello.protocol_versions.max = PROTOCOL_VERSION
        hello.renderer_id = self.RENDERER_ID
        hello.instance_id = str(uuid.uuid4())
        hello.friendly_name = "System test renderer"
        hello.software_version = "0.0.0"
        hello.kind = pb.RENDERER_KIND_NATIVE
        hello.platform.os = "linux"
        return env

    def _envelope(self) -> pb.Envelope:
        self._message_id += 1
        env = pb.Envelope()
        env.message_id = self._message_id
        if self._session_id:
            env.session_id = self._session_id
        return env

    def _send(self, env: pb.Envelope) -> None:
        self._ws.send(env.SerializeToString())

    def _receive(self, timeout: Optional[float]) -> pb.Envelope:
        env = pb.Envelope()
        env.ParseFromString(self._ws.recv(timeout=timeout))
        return env


def _now_ms() -> int:
    return int(time.time() * 1000)
