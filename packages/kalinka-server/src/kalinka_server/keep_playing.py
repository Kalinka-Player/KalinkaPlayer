"""Keep playing: carrying the play queue on with tracks its sources propose.

The queue asks for more (``RequestMoreTracksEvent``) when it starts its last
track while ``QueueConfig.keep_playing`` is on. Every source with tracks in
the queue that reports ``ModuleCapability.CONTINUATION`` is asked at once,
each with its own recently heard tracks as seeds, so a source that has nothing
to offer never stalls playback; their proposals are interleaved, deduplicated
against the queue, and appended here — a module never adds to the queue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.api import PlayQueueController
from kalinka_plugin_sdk.datamodel import EntityId, EntityType, PlayerStateEnum, Track
from kalinka_plugin_sdk.events import (
    KeepPlayingChangedEvent,
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
    RequestMoreTracksEvent,
    TracksAddedEvent,
    TracksRemovedEvent,
)
from kalinka_plugin_sdk.inputmodule import ContinuationRequest, InputModule, ModuleCapability

from .config_model import QueueConfig
from .module_capabilities import capabilities_of
from .queue_limit import QueueLimit

logger = logging.getLogger(__name__.split(".")[-1])

#: How many of a source's last heard tracks seed its continuation.
SEEDS_PER_SOURCE = 5
#: The setting, as a configuration save names it.
SETTING = "base_config.queue.keep_playing"
_UNDER_WAY = (PlayerStateEnum.PLAYING, PlayerStateEnum.PAUSED, PlayerStateEnum.BUFFERING)


@dataclass(frozen=True)
class ContinuationAsk:
    """One source's share of a continuation: who is asked, and with what."""

    source: str
    request: ContinuationRequest


def plan_continuation(
    tracks: Sequence[Track],
    current: Optional[int],
    dislikes: Mapping[str, Sequence[str]],
    count: int,
    from_current_only: bool = False,
) -> List[ContinuationAsk]:
    """The sources to ask for tracks that carry the queue on, most relevant first.

    @param tracks The whole queue, in order.
    @param current Index of the track playing now; None means the last one.
    @param dislikes Per source, the track ids turned down this session.
    @param count The most tracks each source should propose.
    @param from_current_only Seed with the playing track alone and ask only
        its source, for "more like this one".
    @return One ask per source that has a heard track left to seed from: the
        playing track's source first, then the others by how recently they
        were heard. Each ask's seeds are its own tracks, oldest first, never a
        turned-down one; its exclusions are everything of it already queued
        or turned down.
    """
    if not tracks:
        return []
    playing = len(tracks) - 1 if current is None else min(max(current, 0), len(tracks) - 1)
    heard = tracks[: playing + 1]
    if from_current_only:
        heard = heard[-1:]

    queued: Dict[str, List[str]] = {}
    for track in tracks:
        queued.setdefault(track.id.source, []).append(track.id.id)

    by_recency: Dict[str, List[str]] = {}
    for track in reversed(heard):
        turned_down = dislikes.get(track.id.source, ())
        if track.id.id in turned_down:
            continue
        seeds = by_recency.setdefault(track.id.source, [])
        if len(seeds) < SEEDS_PER_SOURCE and track.id.id not in seeds:
            seeds.append(track.id.id)

    asks = []
    for source, newest_first in by_recency.items():
        avoid = list(dict.fromkeys(dislikes.get(source, ())))
        exclude = list(dict.fromkeys(queued.get(source, []) + avoid))
        asks.append(
            ContinuationAsk(
                source=source,
                request=ContinuationRequest(
                    seeds=list(reversed(newest_first)), avoid=avoid, exclude=exclude, count=count
                ),
            )
        )
    return asks


def interleave(proposals: Sequence[Sequence[Track]], queued: set[str], room: int) -> List[Track]:
    """Take turns between the sources' proposals, best first, skipping any
    track already queued or already taken, until ``room`` is filled."""
    taken: List[Track] = []
    seen = set(queued)
    rounds = max((len(p) for p in proposals), default=0)
    for i in range(rounds):
        for proposal in proposals:
            if len(taken) >= room:
                return taken
            if i < len(proposal):
                key = proposal[i].id.to_string
                if key not in seen:
                    seen.add(key)
                    taken.append(proposal[i])
    return taken


class KeepPlayingController:
    """
    @brief Carries the play queue on with tracks its sources propose, and
    keeps what the listener turned down for the rest of the queue's session.

    Built and started by ``InternalModules``; the server's queue routes call
    :meth:`more_like_current`, :meth:`dislike_current` and :meth:`resume`, and
    a save of the setting calls :meth:`announce`. A session ends when the whole
    queue goes at once — a clear or a replace — and its turn-downs go with it.
    Not thread-safe: every method runs on the server's event loop.
    """

    def __init__(
        self,
        config: QueueConfig,
        playqueue: PlayQueueController,
        playqueue_eventbus: EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent],
        module_for: Callable[[str], Optional[InputModule]],
        queue_limit: Callable[[], QueueLimit],
        adding: asyncio.Lock,
    ):
        """
        @param config The live queue settings; read each time, so a save applies at once.
        @param module_for The enabled module behind a source name, or None.
        @param queue_limit The limit in force now (demo mode has its own).
        @param adding The lock the queue routes take around an add, shared so
            that a Keep playing add and a listener's add never pass the limit together.
        """
        self._config = config
        self._playqueue = playqueue
        self._eventbus = playqueue_eventbus
        self._module_for = module_for
        self._queue_limit = queue_limit
        self._adding = adding
        self._continuing = asyncio.Lock()
        self._dislikes: Dict[str, List[str]] = {}
        self._length = 0
        self._listener: Optional[asyncio.Task] = None

    @property
    def dislikes(self) -> Mapping[str, Sequence[str]]:
        """Per source, the track ids turned down this session."""
        return self._dislikes

    async def start(self) -> None:
        """Begin answering the queue; returns once its events are being heard,
        so none sent after it is missed, and the queue's state says whether
        Keep playing is on."""
        self._length = (await self._playqueue.list(0, 1)).total
        subscribed = asyncio.Event()
        self._listener = asyncio.create_task(self._listen(subscribed))
        await subscribed.wait()
        self.announce()

    def announce(self) -> None:
        """Tell every queue client whether Keep playing is on now."""
        self._eventbus.dispatch(KeepPlayingChangedEvent(enabled=self._config.keep_playing))

    async def resume(self) -> int:
        """Carry on a queue that stands at its last track, as Keep playing
        would have, and play on from the first new track if it had finished.

        @return How many tracks were added.
        """
        state = await self._playqueue.get_playback_state()
        total = (await self._playqueue.list(0, 1)).total
        added = await self.extend()
        if added and state.state not in _UNDER_WAY:
            await self._playqueue.play(total)
        return added

    async def shutdown(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
            self._listener = None

    async def extend(self) -> int:
        """Append the configured number of tracks from each source heard in
        the queue, each carrying on from its own tracks, if the queue stands
        at its last track; a queue that has moved on since asking is left
        alone.

        @return How many tracks were added.
        """
        return await self._continue(from_current_only=False, after_current=False, count=None)

    async def more_like_current(self, count: Optional[int] = None) -> int:
        """Queue tracks like the playing one right after it, from its source.

        @param count How many to add; the configured per-source number when None.
        @return How many tracks were added.
        """
        return await self._continue(from_current_only=True, after_current=True, count=count)

    async def dislike_current(self) -> bool:
        """Turn the playing track down for the session and skip it.

        When it was the last track and Keep playing is on, the queue is carried
        on first, steering away from it, so the skip has somewhere to go.

        @return False when nothing is playing.
        """
        state = await self._playqueue.get_playback_state()
        track = state.current_track
        if track is None:
            return False
        turned_down = self._dislikes.setdefault(track.id.source, [])
        if track.id.id not in turned_down:
            turned_down.append(track.id.id)
        if self._config.keep_playing:
            await self.extend()
        await self._playqueue.next()
        return True

    async def _continue(self, from_current_only: bool, after_current: bool, count: Optional[int]) -> int:
        per_source = count or self._config.tracks_per_source
        async with self._continuing:
            queue = await self._playqueue.list(0, self._queue_limit().tracks)
            state = await self._playqueue.get_playback_state()
            if not after_current and (state.index is None or state.index < queue.total - 1):
                return 0
            asks = [
                ask
                for ask in plan_continuation(
                    queue.items, state.index, self._dislikes, per_source, from_current_only
                )
                if self._can_continue(ask.source)
            ]
            if not asks:
                return 0
            proposals = await asyncio.gather(*(self._propose(ask) for ask in asks))
            async with self._adding:
                total = (await self._playqueue.list(0, 1)).total
                room = max(0, self._queue_limit().tracks - total)
                queued = {track.id.to_string for track in queue.items}
                tracks = interleave(proposals, queued, room)
                if not tracks:
                    return 0
                index = state.index + 1 if after_current and state.index is not None else None
                await self._playqueue.add(tracks, index)
        logger.info(
            "Keep playing added %d track(s) from %s",
            len(tracks),
            ", ".join(sorted({t.id.source for t in tracks})),
        )
        return len(tracks)

    def _can_continue(self, source: str) -> bool:
        module = self._module_for(source)
        return module is not None and ModuleCapability.CONTINUATION.value in capabilities_of(module)

    async def _propose(self, ask: ContinuationAsk) -> List[Track]:
        module = self._module_for(ask.source)
        if module is None:
            return []
        try:
            ids = await module.continue_queue(ask.request)
            wanted = [
                EntityId(id=track_id, type=EntityType.TRACK, source=ask.source)
                for track_id in list(dict.fromkeys(ids))[: ask.request.count]
            ]
            if not wanted:
                return []
            items = await module.get_all(wanted)
        except Exception as e:  # noqa: BLE001 — one source failing must not stop the others
            logger.warning("%s could not continue the queue: %s", ask.source, type(e).__name__)
            return []
        resolved = {item.id.to_string: item.track for item in items if item.track is not None}
        return [resolved[e.to_string] for e in wanted if e.to_string in resolved]

    async def _listen(self, subscribed: asyncio.Event) -> None:
        while True:
            try:
                async with self._eventbus.stream(
                    [
                        PlayQueueEventType.RequestMoreTracks,
                        PlayQueueEventType.TracksAdded,
                        PlayQueueEventType.TracksRemoved,
                    ]
                ) as stream:
                    subscribed.set()
                    async for event in stream:
                        await self._on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Keep playing event listener failed: %s", e, exc_info=True)
                await asyncio.sleep(1)

    async def _on_event(self, event: object) -> None:
        if isinstance(event, TracksAddedEvent):
            self._length += len(event.tracks)
        elif isinstance(event, TracksRemovedEvent):
            if len(event.indices) >= self._length:
                self._dislikes.clear()
            self._length = max(0, self._length - len(event.indices))
        elif isinstance(event, RequestMoreTracksEvent):
            await self.extend()
