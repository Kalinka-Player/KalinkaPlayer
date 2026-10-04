"""What the play queue calls playback state, and how a renderer snapshot maps
onto it.

The vocabulary is the embedded AudioPlayer's, kept unchanged when playback
moved out of process: the queue reasons in these terms and does not know a
renderer is involved. It lives here rather than with the renderer player so
that the queue does not have to import the renderer to name its own states.

:func:`from_snapshot` is the whole translation from the wire's merged snapshot
dict (see :mod:`renderer_state`) into a ``StreamState`` — a pure function, so
it is testable without a session or a player.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AudioGraphNodeState(Enum):
    """Playback states, mirroring the native player's enum of the same name."""

    ERROR = -1
    STOPPED = 0
    PREPARING = 1
    STREAMING = 2
    PAUSED = 3
    FINISHED = 4
    SOURCE_CHANGED = 5


class StreamErrorSource(Enum):
    NONE = 0
    HTTP_STREAM = 1
    AUDIO_OUTPUT = 2
    DECODER = 3


class DeviceAccess(Enum):
    """How the renderer holds the output device. SHARED is not proof that
    anything alters the samples, only that nothing rules it out."""

    UNKNOWN = 0
    EXCLUSIVE = 1
    SHARED = 2


@dataclass
class AudioFormatInfo:
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0
    sample_format: str = ""


@dataclass
class DeviceInfo:
    format: AudioFormatInfo = field(default_factory=AudioFormatInfo)
    access: DeviceAccess = DeviceAccess.UNKNOWN


@dataclass
class StreamInfo:
    format: AudioFormatInfo = field(default_factory=AudioFormatInfo)
    # The output side; differs from `format` when the device resamples or
    # widens the sample format.
    device: Optional[DeviceInfo] = None
    # None, never 0, when the renderer cannot tell: 0 reads as "already ended".
    duration_ms: Optional[int] = None

    @property
    def lossless_path(self) -> bool:
        """Whether the decoded samples reach the device unaltered.

        Exclusivity carries this: a shared device reports the format its plugin
        was opened at, which says nothing about what the card ends up running,
        so no comparison can rescue it. Comparing the formats then catches a
        renderer whose device took something other than what was decoded.

        The volume is deliberately not part of it — it is reported with the
        device that applies it, where it is current.
        """
        if self.device is None or self.device.access is not DeviceAccess.EXCLUSIVE:
            return False
        decoded, device = self.format, self.device.format
        # Zero is "not reported", and two silences are not an agreement.
        if min(decoded.sample_rate, decoded.bits_per_sample, decoded.channels) <= 0:
            return False
        if decoded.sample_format == "DSD":
            bits = {
                "DSD_U8": 8,
                "DSD_U16_LE": 16,
                "DSD_U16_BE": 16,
                "DSD_U32_LE": 32,
                "DSD_U32_BE": 32,
                "DoP_S24_LE": 16,
                "DoP_S24_3LE": 16,
                "DoP_S32_LE": 16,
            }.get(device.sample_format, 0)
            return (
                bits > 0
                and decoded.sample_rate == device.sample_rate * bits
                and decoded.channels == device.channels
            )
        return (
            device.sample_rate == decoded.sample_rate
            and device.bits_per_sample == decoded.bits_per_sample
            and device.channels == decoded.channels
        )


@dataclass
class StreamError:
    source: StreamErrorSource = StreamErrorSource.NONE
    message: str = ""


@dataclass
class StreamState:
    state: AudioGraphNodeState
    # Position when the state was reported; timestamp is the local receipt
    # time (monotonic ns), so extrapolation needs no cross-machine clock.
    position: int = 0
    timestamp: int = 0
    error: Optional[StreamError] = None
    stream_info: Optional[StreamInfo] = None
    # Which appended stream this is about, None when the renderer named none.
    stream_id: Optional[int] = None

    def position_at(self, now_ns: int) -> int:
        """Where playback has reached: only a running stream advances."""
        if self.state is not AudioGraphNodeState.STREAMING:
            return self.position
        return self.position + max(0, (now_ns - self.timestamp) // 1_000_000)


_STATE_NAMES = {
    "stopped": AudioGraphNodeState.STOPPED,
    "preparing": AudioGraphNodeState.PREPARING,
    "playing": AudioGraphNodeState.STREAMING,
    "paused": AudioGraphNodeState.PAUSED,
    "finished": AudioGraphNodeState.FINISHED,
    "error": AudioGraphNodeState.ERROR,
}

_DEVICE_ACCESS = {
    "exclusive": DeviceAccess.EXCLUSIVE,
    "shared": DeviceAccess.SHARED,
}

_ERROR_SOURCES = {
    "none": StreamErrorSource.NONE,
    "http_stream": StreamErrorSource.HTTP_STREAM,
    "audio_output": StreamErrorSource.AUDIO_OUTPUT,
    "decoder": StreamErrorSource.DECODER,
}


def to_audio_format(fmt: Optional[dict]) -> Optional[AudioFormatInfo]:
    if not fmt:
        return None
    return AudioFormatInfo(
        sample_rate=fmt.get("sample_rate_hz", 0),
        channels=fmt.get("channels", 0),
        bits_per_sample=fmt.get("bits_per_sample", 0),
        sample_format=fmt.get("sample_format", ""),
    )


def to_device_info(info: Optional[dict]) -> Optional[DeviceInfo]:
    if not info:
        return None
    return DeviceInfo(
        format=to_audio_format(info.get("format")) or AudioFormatInfo(),
        access=_DEVICE_ACCESS.get(info.get("access") or "", DeviceAccess.UNKNOWN),
    )


def to_stream_info(snapshot: dict) -> Optional[StreamInfo]:
    """A renderer may know the decoded format, the device's, or only the
    length, so any one of the three is enough to report something."""
    decoded = to_audio_format(snapshot.get("format"))
    device = to_device_info(snapshot.get("device_info"))
    duration_ms = snapshot.get("duration_ms")
    if decoded is None and device is None and duration_ms is None:
        return None
    return StreamInfo(
        format=decoded or AudioFormatInfo(),
        device=device,
        duration_ms=duration_ms,
    )


def to_error(error: Optional[dict]) -> Optional[StreamError]:
    if not error:
        return None
    return StreamError(
        source=_ERROR_SOURCES.get(error.get("source") or "", StreamErrorSource.NONE),
        message=error.get("message") or "",
    )


def to_stream_id(source_token: Optional[str]) -> Optional[int]:
    """The stream id a source token names — the inverse of what the renderer
    player stringifies on the way out. Anything else came from elsewhere."""
    try:
        return int(source_token)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def from_snapshot(snapshot: dict) -> Optional[StreamState]:
    """The queue's view of a renderer snapshot, or None when the renderer has
    not reported a state we play by (``unspecified``, before anything ran)."""
    state = _STATE_NAMES.get(snapshot.get("playback_state") or "")
    if state is None:
        return None
    return StreamState(
        state=state,
        position=snapshot.get("position_ms", 0),
        timestamp=time.monotonic_ns(),
        error=to_error(snapshot.get("error")),
        stream_info=to_stream_info(snapshot),
        stream_id=to_stream_id(snapshot.get("source_token")),
    )


class StateMonitor:
    """Async-iterable stream of StreamState, in place of the native monitor."""

    _STOP = object()

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = True

    def push(self, state: StreamState) -> None:
        if self._running:
            self._queue.put_nowait(state)

    def stop(self) -> None:
        self._running = False
        self._queue.put_nowait(self._STOP)

    def is_running(self) -> bool:
        return self._running

    def __aiter__(self):
        return self

    async def __anext__(self) -> StreamState:
        if not self._running:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is self._STOP:
            raise StopAsyncIteration
        return item
