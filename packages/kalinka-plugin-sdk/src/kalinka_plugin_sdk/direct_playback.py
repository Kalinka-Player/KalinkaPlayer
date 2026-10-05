"""Playing on the renderer directly, outside the play queue (SDK 3.4+).

For an input plugin that receives playback from elsewhere — a Connect
receiver taking commands from a phone app — rather than serving tracks the
play queue asks for. The plugin *acquires* the output the server currently
plays through and drives it until it releases it or loses it. The play queue
keeps its contents meanwhile, and clients show the plugin's playback in its
place, badged with the title the plugin acquired under.

The hold is exclusive and the listener decides nothing about its end: the
queue takes the output back the moment the listener plays from it, and the
plugin is told. A plugin reports that to wherever its playback came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from .datamodel import DeviceVolume, PlaybackState, Track
from .inputmodule import TrackSource


class RevokeReason(str, Enum):
    """Why a hold ended without the plugin releasing it."""

    QUEUE_PLAY = "queue_play"  # the listener played from the play queue
    TAKEN_BACK = "taken_back"  # the listener stopped this playback from a client
    OTHER_HOLDER = "other_holder"  # another plugin or the speaker test took the output
    OUTPUT_LOST = "output_lost"  # the renderer went away or ended the session
    IDLE = "idle"  # stopped or paused for too long
    SHUTDOWN = "shutdown"


class TransportKind(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    NEXT = "next"
    PREV = "prev"
    SEEK = "seek"


@dataclass(frozen=True)
class TransportRequest:
    """A transport control a client sent while the plugin holds the output.

    The server does not apply it: the plugin does, so that whatever its
    playback came from stays in step. ``position_ms`` is set for SEEK only.
    """

    kind: TransportKind
    position_ms: Optional[int] = None


@dataclass(frozen=True)
class OutputCapabilities:
    """SDK 3.9: what the output acquire() would take can play, as the server knows it.

    ``dsd`` is True while the renderer is set to output DSD, False when it is
    not or has no such setting, and None when the server could not ask it.
    """

    dsd: Optional[bool] = None


class OutputUnavailable(RuntimeError):
    """No renderer can be played through right now; the message says why."""


class HoldEnded(RuntimeError):
    """The hold was released or revoked; acquire again to play."""


class DirectPlaybackListener(Protocol):
    """What the server tells the holding plugin. Each method may be async.

    Calls arrive in order, one at a time, and each is cut off after the same
    per-call budget as an InputModule call, so none may wait on the network.
    """

    def on_state(self, state: PlaybackState) -> Any:
        """The playback clients are shown, as it changes."""
        ...

    def on_finished(self) -> Any:
        """The current source played to its end without a queued successor."""
        ...

    def on_next_started(self, track: Track) -> Any:
        """SDK 3.6: the queued track started; on_state follows with its progress.

        Only called for a source supplied through set_next(). The server
        already advanced playback; the plugin must not play it again.
        """
        ...

    def on_command(self, request: TransportRequest) -> Any:
        """A client asked for a transport control."""
        ...

    def on_revoked(self, reason: RevokeReason) -> Any:
        """The hold ended without the plugin releasing it. Called once."""
        ...

    def on_volume(self, volume: DeviceVolume) -> Any:
        """The output's volume, from whichever device controls it.

        Once as the hold starts, then on every change: a client's, the
        plugin's own set_volume echoed back, or a knob on the device itself.
        """
        ...


class DirectPlaybackSession(Protocol):
    """One hold on the output. Every call raises HoldEnded once it has ended."""

    @property
    def active(self) -> bool:
        """False once the hold has been released or revoked."""
        ...

    @property
    def renderer_id(self) -> Optional[str]:
        """The renderer the hold plays through."""
        ...

    @property
    def state(self) -> PlaybackState:
        """The playback clients are shown now."""
        ...

    async def play(
        self, source: TrackSource, track: Track, *, start_offset_ms: int = 0
    ) -> None:
        """Replace what plays with ``source``, shown to clients as ``track``.

        Raises ValueError for a start offset on a sequential source, which
        plays from its first byte.
        """
        ...

    async def set_next(
        self, source: Optional[TrackSource], track: Optional[Track] = None
    ) -> None:
        """SDK 3.6: set one successor, replacing any pending one.

        Call after play(). The server submits it to the renderer immediately,
        regardless of duration. The renderer advances automatically;
        on_next_started reports this. Pause and seek keep the queued source.
        None, play(), and stop() remove any queued successor.
        A track is required with a source; sequential sources cannot be queued.
        """
        ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None:
        """SDK 3.6: stop the source while keeping the renderer session open.

        A following play reuses the session and its volume. The server's
        normal idle timeout still releases it; release() gives it up now.
        """
        ...

    async def seek(self, position_ms: int) -> None:
        """Raises ValueError while a sequential source plays: play a new one."""
        ...

    async def set_volume(self, percent: int) -> None:
        """Through whichever device controls the renderer's volume.

        ``percent`` is of that device's range; get_volume() and on_volume()
        report in the device's own steps, with its maximum.
        """
        ...

    async def get_volume(self) -> Optional[DeviceVolume]:
        """None when nothing controls the volume."""
        ...

    async def release(self) -> None:
        """Stop and give the output back to the play queue. Idempotent."""
        ...


class DirectPlayback(Protocol):
    """Offered on InputPluginContext.direct_playback by servers that support it."""

    async def acquire(
        self, title: str, listener: DirectPlaybackListener
    ) -> DirectPlaybackSession:
        """Take the output from whoever plays through it.

        ``title`` is what clients badge the playback with ("Qobuz Connect").
        A second acquire by the same plugin ends its first hold.

        @throw OutputUnavailable when no renderer can be played through.
        """
        ...

    async def output_capabilities(self) -> OutputCapabilities:
        """SDK 3.9: what the output acquire() would take can play right now.

        Asks the renderer without taking the output, so call it when the
        answer is needed rather than on every state update. Never raises: a
        renderer that cannot be asked yields fields of None.
        """
        ...
