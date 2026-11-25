"""Play history: which of the library's tracks the listener heard, and how much.

Album recommendations are drawn from it: a track played out says "more like
this", one skipped early says "not this".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from kalinka_plugin_sdk.datamodel import PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.events import PlaybackStateChangedEvent, PlayQueueEventType

logger = logging.getLogger(__name__.split(".")[-1])

#: A play counts as heard out when it ran this share of the track.
COMPLETE_SHARE = 0.9
#: A play that stopped sooner than this, and before half the track, was a skip.
SKIP_BEFORE_S = 30


@dataclass(frozen=True)
class PlayRecord:
    """One finished play of a library track."""

    track_id: str
    played_at: int
    listened_s: int
    completed: bool
    skipped: bool


@dataclass
class _Play:
    play_id: str
    track_id: str
    duration_s: int
    started_at: float
    listened_s: float = 0.0
    playing_since: Optional[float] = None


def judge(track_id: str, played_at: float, duration_s: int, listened_s: float) -> PlayRecord:
    """Whether a play of ``listened_s`` seconds of a ``duration_s`` track was
    heard out, skipped, or neither."""
    completed = duration_s > 0 and listened_s >= COMPLETE_SHARE * duration_s
    skip_before = min(SKIP_BEFORE_S, duration_s / 2) if duration_s > 0 else SKIP_BEFORE_S
    return PlayRecord(
        track_id=track_id,
        played_at=int(played_at),
        listened_s=int(round(listened_s)),
        completed=completed,
        skipped=not completed and listened_s < skip_before,
    )


class PlayTracker:
    """
    @brief Turns the queue's playback states into finished plays of one
    source's tracks.

    Feed it every playback state in order. A play is one ``play_id``; it ends
    when another begins or nothing plays, and only the time spent PLAYING
    counts as listened. Tracks of other sources are not followed.
    """

    def __init__(self, source: str, clock: Callable[[], float] = time.time):
        """
        @param source The module whose tracks are followed.
        @param clock Wall-clock seconds; injected so tests can drive time.
        """
        self._source = source
        self._clock = clock
        self._play: Optional[_Play] = None

    def feed(self, state: PlaybackState) -> Optional[PlayRecord]:
        """Take the next playback state.

        @return The play this state ended, if one of the source's did.
        """
        now = self._clock()
        finished = None
        if self._play is not None and state.play_id != self._play.play_id:
            finished = self._finish(now)
        if self._play is None and state.play_id is not None:
            track = state.current_track
            if track is not None and track.id.source == self._source:
                self._play = _Play(
                    play_id=state.play_id,
                    track_id=track.id.id,
                    duration_s=track.duration or 0,
                    started_at=now,
                )
        if self._play is not None:
            self._account(state.state, now)
        return finished

    def _account(self, state: Optional[PlayerStateEnum], now: float) -> None:
        play = self._play
        if state == PlayerStateEnum.PLAYING:
            if play.playing_since is None:
                play.playing_since = now
        elif play.playing_since is not None:
            play.listened_s += now - play.playing_since
            play.playing_since = None

    def _finish(self, now: float) -> PlayRecord:
        play = self._play
        self._play = None
        if play.playing_since is not None:
            play.listened_s += now - play.playing_since
        return judge(play.track_id, play.started_at, play.duration_s, play.listened_s)


class PlayHistoryRecorder:
    """
    @brief Follows the play queue and stores every finished play of the
    library's tracks.

    Runs as one task for the module's lifetime; cancel it to stop.
    """

    def __init__(self, listener, record: Callable[[PlayRecord], None], source: str = "localfiles"):
        """
        @param listener The plugin context's play queue event listener.
        @param record Stores a finished play; called off the event loop.
        """
        self._listener = listener
        self._record = record
        self._tracker = PlayTracker(source)

    async def run(self) -> None:
        while True:
            try:
                async with self._listener.stream([PlayQueueEventType.PlaybackStateChanged]) as stream:
                    async for event in stream:
                        if isinstance(event, PlaybackStateChangedEvent):
                            finished = self._tracker.feed(event.state)
                            if finished is not None:
                                await asyncio.to_thread(self._record, finished)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Play history listener failed: %s", e, exc_info=True)
                await asyncio.sleep(1)
