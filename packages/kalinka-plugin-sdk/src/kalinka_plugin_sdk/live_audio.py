"""SDK 3.21: receiver audio, independent of the selected renderer.

Producers publish the PCM their receiver already supplies. Core owns retention,
HTTP framing and output routes; a plugin never compresses or spools this media.
All producer methods run on the server's event loop and never wait for output.
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

PCM_MIME_TYPE = "application/vnd.kalinka.pcm"
# Wire identifiers, not ALSA enum ordinals. Layouts are interleaved, little endian.
PCM_FORMATS = {
    "S16_LE": (1, 2, 16),
    "S24_3LE": (2, 3, 24),
    "S24_LE": (3, 4, 24),
    "S32_LE": (4, 4, 32),
    "FLOAT_LE": (5, 4, 32),
}


@dataclass(frozen=True)
class PcmFormat:
    sample_rate: int
    channels: int = 2
    sample_format: str = "S16_LE"

    def __post_init__(self):
        if not 8000 <= self.sample_rate <= 768000:
            raise ValueError("PCM rate must be between 8 kHz and 768 kHz")
        if self.channels not in (1, 2):
            raise ValueError("live PCM supports mono or stereo")
        if self.sample_format not in PCM_FORMATS:
            raise ValueError(f"unsupported live PCM layout: {self.sample_format}")

    @property
    def frame_bytes(self) -> int:
        return self.channels * PCM_FORMATS[self.sample_format][1]


@dataclass(frozen=True)
class LivePolicy:
    """Time bounds, not independent buffers to fill at every stage."""

    target_latency_ms: int = 60
    max_buffer_ms: int = 250

    def __post_init__(self):
        if not 20 <= self.target_latency_ms <= 500:
            raise ValueError("live target latency must be 20..500 ms")
        if not self.target_latency_ms < self.max_buffer_ms <= 2000:
            raise ValueError("live buffer ceiling must exceed target and be <= 2000 ms")


class LiveAudioRef(BaseModel):
    """Opaque Core source identity; only play_live() may bind this reference."""

    live_source_id: str


class LiveSource(Protocol):
    @property
    def reference(self) -> LiveAudioRef: ...

    @property
    def format(self) -> PcmFormat: ...

    @property
    def closed(self) -> bool: ...

    def offer(self, pcm: bytes) -> None:
        """Publish whole frames without blocking. Old retained audio is evicted.

        Empty blocks do nothing. A partial frame raises ValueError; the receiver
        adapter must carry its partial reads into its next call.
        """
        ...

    def reset(self, format: PcmFormat | None = None) -> None:
        """Start a fresh timeline after a flush/seek or a PCM format change.

        Existing readers end; an active live hold rebinds at the new live edge.
        Metadata-only changes do not need a reset.
        """
        ...

    def close(self) -> None:
        """End capture, retire all routes and release memory. Idempotent."""
        ...


class LiveAudio(Protocol):
    def create_pcm(self, format: PcmFormat, *, max_buffer_ms: int = 200) -> LiveSource:
        """Create a bounded memory source owned by this plugin.

        The retention ceiling does not delay playback or require a startup
        fill. The plugin closes each source when its receiver session ends.
        """
        ...
