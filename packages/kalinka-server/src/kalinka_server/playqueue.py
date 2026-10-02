import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable
from typing import Callable, Optional

from kalinka_serialized.serial_executor import interrupt

from .config_model import KalinkaConfig

from kalinka_serialized import (
    ResolutionSlot,
    SerialExecutor,
    serialised,
    with_serial_executor,
)

from kalinka_plugin_sdk.datamodel import (
    EntityId,
    PlaybackControl,
    PlayerStateEnum,
    Track,
    PlaybackState,
    PlaybackMode,
    TrackList,
)
from kalinka_plugin_sdk.direct_playback import (
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_sdk.events import (
    PlayQueueState,
    PlaybackErrorEvent,
    PlaybackModeChangedEvent,
    RequestMoreTracksEvent,
    TracksAddedEvent,
    TracksRemovedEvent,
    TrackMovedEvent,
    TrackUnavailableEvent,
)
from kalinka_plugin_sdk.inputmodule import (
    SourceUnavailableError,
    TrackInfo,
    TrackSource,
)

from kalinka_plugin_sdk.api import PlayQueueController, EventEmitter

from .playback_arbiter import PlaybackArbiter
from .playback_view import to_audio_info, to_state_name
from .renderer_player import RendererPlayer
from .stream_state import (
    AudioGraphNodeState,
    StateMonitor,
    StreamErrorSource,
    StreamState,
)
from .renderer_registry import RendererRegistry, RendererUnavailable
from .renderer_sessions import SessionPool
from .tasks import detach


logger = logging.getLogger(__name__.split(".")[-1])

PREFETCH_TIME_MS = 5000

# Upper bound for a single source_retriever() call. Plugins set their own (smaller)
# HTTP timeouts; this is a backstop so a misbehaving plugin can never pin the
# resolution slot indefinitely. Generous enough to allow one in-plugin retry.
SOURCE_RETRIEVAL_TIMEOUT_S = 8


def _remap_index(idx: int, from_index: int, to_index: int) -> int:
    """Remap an index after moving a track from from_index to to_index.

    Mirrors the effect of: list.pop(from_index); list.insert(to_index, item).
    """
    if idx == from_index:
        return to_index
    if from_index < to_index:  # track moved forward
        if from_index < idx <= to_index:
            return idx - 1
    else:  # track moved backward
        if to_index <= idx < from_index:
            return idx + 1
    return idx


# State restore can involve many I/O calls; keep it outside the serial executor
# so startup restore does not block the command lane.
@with_serial_executor
class PlayQueueImpl(PlayQueueController):
    def __init__(
        self,
        config: KalinkaConfig,
        event_emitter: EventEmitter,
        renderer_registry: RendererRegistry,
        renderer_sessions: SessionPool,
        *,
        arbiter: Optional[PlaybackArbiter] = None,
    ):
        super().__init__()
        self.event_emitter = event_emitter
        self._config = config
        self._registry = renderer_registry
        self._sessions = renderer_sessions
        # Playback state goes to clients through the arbiter, which drops it
        # while a plugin plays in the queue's place.
        self._arbiter = arbiter or PlaybackArbiter(event_emitter)
        self._arbiter.set_queue(self)
        # One monitor for the queue's whole life; players come and go behind
        # it, so switching renderers never re-points the state listener.
        self.state_monitor = StateMonitor()
        # The native monitor reported the current state on subscription; the
        # initial STOPPED event to clients relies on it.
        self.state_monitor.push(
            StreamState(
                state=AudioGraphNodeState.STOPPED, timestamp=time.monotonic_ns()
            )
        )
        self._track_player = self._new_player()
        self.current_track_id = 0
        self.current_format = None
        self.track_list: list[TrackInfo] = []

        # Playback mode
        self.shuffle = False  # TODO
        self.repeat_single = False
        self.repeat_all = False

        self._prefetch_task = None
        self.prepared_tracks: OrderedDict = OrderedDict()
        # The queue owns stream-id allocation; the player only tags.
        self._next_stream_id = 0
        # StreamId of the stream currently being played (popped from prepared_tracks on SOURCE_CHANGED)
        self.current_stream_id: Optional[int] = None
        self._retry_attempted: bool = False
        # Set to True when SOURCE_CHANGED is expected from a retry, to avoid resetting _retry_attempted
        self._retry_pending: bool = False
        # Indices of tracks whose source could not be retrieved. Mirrors the
        # `unavailable` flag clients see; used to emit TrackUnavailableEvent only
        # on real state changes. Kept in sync with track_list across mutations.
        self._unavailable_indices: set[int] = set()

        # Single in-flight source resolution; see "Off-lane source resolution" below.
        self._resolution = ResolutionSlot()

        self._state_update_task = None

    async def __aenter__(self):
        """Start the state update listener. Call this after initialization."""

        self._state_update_task = asyncio.create_task(
            self._state_update_listener_async()
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop the play queue and cleanup resources."""
        self._resolution.supersede()
        self._cancel_prefetch_timer()
        await self._track_player.shutdown()
        self.state_monitor.stop()
        if self._state_update_task:
            self._state_update_task.cancel()
            try:
                await self._state_update_task
            except asyncio.CancelledError:
                pass

    async def _state_update_listener_async(self):
        """Listen for state changes using async iterator."""
        try:
            async for new_state in self.state_monitor:
                logger.info(f"New state: {new_state}")
                try:
                    await self._process_state_update(new_state)
                except TimeoutError as e:
                    logger.error(
                        f"State update processing timed out: {e}. "
                        "Continuing to listen for state changes."
                    )
                except Exception as e:
                    logger.error(
                        f"Error processing state update: {e}. "
                        "Continuing to listen for state changes.",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.debug("State listener cancelled")
            raise

    @interrupt
    async def _process_state_update(self, new_state):
        if new_state.state == AudioGraphNodeState.SOURCE_CHANGED:
            if self._retry_pending:
                # SOURCE_CHANGED caused by a retry — keep _retry_attempted=True so we don't retry again
                self._retry_pending = False
            else:
                self._retry_attempted = False
            self._adopt_source(new_state.stream_id)
            return
        elif new_state.state == AudioGraphNodeState.ERROR:
            if (
                not self._retry_attempted
                and new_state.error is not None
                and new_state.error.source == StreamErrorSource.HTTP_STREAM
            ):
                self._retry_attempted = True
                failed_position_ms = new_state.position
                logger.info(
                    "HTTP stream error at %d ms, retrying with fresh source",
                    failed_position_ms,
                )
                await self._retry_current_track_async(failed_position_ms)
                return
            self._cancel_prefetch_timer()
        elif new_state.state == AudioGraphNodeState.FINISHED:
            # Only auto-play next if no streams are already queued and no
            # resolution is already in flight (a manual switch). Non-empty
            # prepared_tracks means a manual switch already appended a new
            # stream — SOURCE_CHANGED will handle the transition.
            #
            # current_stream_id is None only when there is no track actually
            # playing — i.e. this FINISHED came from tearing the graph down
            # (clear()/clear_all() disconnects the last node and the switcher
            # emits FINISHED), not from a track running to its end. Advancing
            # then would spuriously start current_track_id + 1 (index 1 of a
            # freshly-added queue), so require a live current stream.
            if (
                self.current_stream_id is not None
                and self._prefetch_task is None
                and not self.prepared_tracks
                and not self._resolution.active
            ):
                target = self.current_track_id + 1
                self._begin_resolution(
                    target,
                    resolve=lambda: self._resolve_playable(target, 1),
                    next_step=self._play_resolved,
                )
        elif new_state.state == AudioGraphNodeState.STOPPED:
            # The prefetch may have completed and appended a stream just as the
            # player ran out of time and stopped.  If a stream is already
            # queued and the player is still stopped (i.e. play_next hasn't
            # restarted it yet), kick the player back into motion.
            if (
                self.prepared_tracks
                and not self._resolution.active
                and self._track_player.get_state().state == AudioGraphNodeState.STOPPED
            ):
                next_idx = next(iter(self.prepared_tracks))
                self._begin_resolution(
                    next_idx,
                    resolve=lambda: self._resolve_playable(next_idx, 1),
                    next_step=self._play_resolved,
                )
                return  # resolution started; its own state events will follow
            self._cancel_prefetch_timer()
        elif new_state.state == AudioGraphNodeState.STREAMING:
            self._setup_prefetch_timer(new_state)
        else:
            self._cancel_prefetch_timer()

        if new_state.state == AudioGraphNodeState.FINISHED:
            new_state.position = 0

        self._arbiter.report(self, self._to_playback_state(new_state))

    @serialised
    async def play(self, index: Optional[int] = None) -> None:
        # Index-addressed entry point: reject out-of-range indices instead of
        # letting _resolve_playable wrap them (which is only intended for the
        # ±1 boundary stepping done by next()/prev()).
        if index is not None and index not in range(0, len(self.track_list)):
            return
        if len(self.track_list) == 0:
            return
        target = self.current_track_id if index is None else index
        self._begin_resolution(
            target,
            resolve=lambda: self._resolve_playable(target, 1),
            next_step=self._play_resolved,
        )

    @serialised
    async def play_next(self, index: int) -> None:
        # play_next is index-addressed; an out-of-range or already-queued index
        # is a no-op. (Prefetch wraps via _play_next_track_async before calling.)
        if len(self.track_list) == 0:
            return
        if index not in range(0, len(self.track_list)) or index in self.prepared_tracks:
            return
        logger.info(f"Prefetching next track index={index}")
        self._begin_resolution(
            index,
            resolve=lambda: self._resolve_playable(index, 1),
            next_step=self._prefetch_resolved,
            cancel_prefetch=False,  # this *is* the prefetch; don't cancel its timer
        )

    # ------------------------------------------------------------------
    # Off-lane source resolution
    #
    # source_retriever() is the only slow (network) operation in the playqueue.
    # Running it inside the serial executor would freeze every other command and
    # the state-update interrupt lane while a streaming plugin times out. So each
    # command splits in two: a serialised entry point that kicks off the (slow)
    # ``resolve`` off the lane, and a serialised ``next_step`` that applies the
    # result back on the lane once the source is in hand. Only one resolution runs
    # at a time; a new one cancels the previous, which then returns silently
    # (no TrackUnavailableEvent).
    # ------------------------------------------------------------------

    def _begin_resolution(
        self,
        target: int,
        resolve: Callable[[], Awaitable],
        next_step: Callable[[int, object], Awaitable[None]],
        cancel_prefetch: bool = True,
    ) -> None:
        """Cancel any in-flight resolution and start a new one. Returns at once.

        ``resolve`` runs the source fetch off the serial lane; ``next_step`` is a
        serialised method invoked with ``(gen, result)`` to apply it. Must be
        called from the serial (or interrupt) lane so the cancel + swap of the
        single resolution slot is atomic with respect to other commands.
        """
        if cancel_prefetch:
            self._cancel_prefetch_timer()
        self._resolution.start(
            lambda gen: self._drive_resolution(gen, resolve, next_step),
            target=target,
        )

    async def _drive_resolution(self, gen, resolve, next_step) -> None:
        """Run ``resolve`` off-lane, then hand its result to ``next_step``.

        Cancellation (a superseding command or an affecting structural mutation)
        propagates out silently: no events are emitted and nothing is applied.
        """
        try:
            result = await resolve()
            await next_step(gen, result)
        except Exception:
            # CancelledError (a superseding command or affecting mutation) is a
            # BaseException and propagates silently; only real errors are logged.
            logger.exception("Source resolution task failed")

    def _accept_scan(self, gen: int, result):
        """Fence a scan result, flag the failed indices, return the playable
        ``(index, track_source)`` to apply — or None if superseded / nothing
        playable. Runs on the lane (called from a serialised next_step)."""
        if not self._resolution.is_current(gen):
            return None
        self._resolution.finish(gen)
        index, track_source, track_ref, failed = result
        # Flag failures only now, on the lane, so the marks (and their dedup
        # state) stay consistent with mutations that remap _unavailable_indices.
        for i, reason in failed:
            self._set_track_unavailable(i, True, reason)
        if track_source is None or index is None:
            return None
        # A multi-step scan can resolve an index *past* the start target, and a
        # structural edit in the (target, index] band isn't covered by
        # cancel_if (its predicate keys on the start target). Re-validate that
        # the resolved index still points at the track we fetched; if it shifted
        # under us, bail silently and let the next command re-resolve.
        if index >= len(self.track_list) or self.track_list[index] is not track_ref:
            return None
        self._set_track_unavailable(index, False)
        return index, track_source

    @serialised
    async def _play_resolved(self, gen, result) -> None:
        playable = self._accept_scan(gen, result)
        if playable is not None:
            self._apply_play(*playable)

    @serialised
    async def _prefetch_resolved(self, gen, result) -> None:
        playable = self._accept_scan(gen, result)
        if playable is not None:
            self._apply_prefetch(*playable)

    def _new_stream_id(self) -> int:
        sid = self._next_stream_id
        self._next_stream_id += 1
        return sid

    def _adopt_source(self, stream_id: Optional[int]) -> None:
        """Take up the stream the renderer switched to.

        It names the stream, so a switch we did not watch happen — one the
        renderer made while it was unreachable, or one that skipped a stream it
        could not open — lands on the track that is really playing rather than
        on whatever sits at the head of the prepared map.
        """
        if stream_id is not None and stream_id == self.current_stream_id:
            return
        index = self._prepared_index_of(stream_id)
        if index is None:
            if not self.prepared_tracks:
                return
            index = next(iter(self.prepared_tracks))
        entry = None
        while self.prepared_tracks:
            popped, entry = self.prepared_tracks.popitem(last=False)
            if popped == index:
                break
        if entry is None:
            return
        track_source, self.current_stream_id = entry
        self.current_track_id = index
        self.current_format = track_source.format
        self._request_more_tracks()

    def _prepared_index_of(self, stream_id: Optional[int]) -> Optional[int]:
        if stream_id is None:
            return None
        for index, (_, prepared_id) in self.prepared_tracks.items():
            if prepared_id == stream_id:
                return index
        return None

    def _clear_prepared_streams(self) -> None:
        """Remove every prefetched stream and empty the prepared map."""
        for _, (_, stream_id) in list(self.prepared_tracks.items()):
            self._track_player.remove(stream_id)
        self.prepared_tracks.clear()

    def _apply_play(self, index: int, track_source: TrackSource) -> None:
        """Replace current playback with the already-resolved track."""
        self._retry_attempted = False
        self._cancel_prefetch_timer()

        self._clear_prepared_streams()

        # Append new stream — auto-starts. prepared_tracks is non-empty so the
        # FINISHED handler (triggered by the removals above) will not auto-play.
        stream_id = self._new_stream_id()
        self._track_player.append(stream_id, track_source)
        self.prepared_tracks[index] = (track_source, stream_id)

        if self.current_stream_id is not None:
            self._track_player.remove(self.current_stream_id)
            self.current_stream_id = None

    def _apply_prefetch(self, index: int, track_source: TrackSource) -> None:
        """Append the already-resolved track as the upcoming stream."""
        # Resolution may have landed on a now-queued index, or the player may
        # have stopped while we were fetching — fall back to a full replace.
        if index in self.prepared_tracks:
            return
        if self._track_player.get_state().state == AudioGraphNodeState.STOPPED:
            self._apply_play(index, track_source)
            return
        stream_id = self._new_stream_id()
        self._track_player.append(stream_id, track_source)
        self.prepared_tracks[index] = (track_source, stream_id)

    @serialised
    async def pause(self, paused: bool):
        kind = TransportKind.PAUSE if paused else TransportKind.RESUME
        if self._arbiter.forward(TransportRequest(kind)):
            return
        if paused:
            self._track_player.pause()
        else:
            self._track_player.resume()

    @serialised
    async def next(self):
        if self._arbiter.forward(TransportRequest(TransportKind.NEXT)):
            return
        if len(self.track_list) == 0:
            return
        target = self.current_track_id + 1
        self._begin_resolution(
            target,
            resolve=lambda: self._resolve_playable(target, 1),
            next_step=self._play_resolved,
        )

    @serialised
    async def prev(self):
        if self._arbiter.forward(TransportRequest(TransportKind.PREV)):
            return
        if len(self.track_list) == 0:
            return
        target = self.current_track_id - 1
        self._begin_resolution(
            target,
            resolve=lambda: self._resolve_playable(target, -1),
            next_step=self._play_resolved,
        )

    @serialised
    async def seek(self, position_ms: int) -> None:
        if self._arbiter.forward(
            TransportRequest(TransportKind.SEEK, position_ms=position_ms)
        ):
            return position_ms
        return self._track_player.seek(position_ms)

    @serialised
    async def stop(self):
        if self._arbiter.held_by_plugin:
            # Detached: the arbiter's lock may be held by an acquire that is
            # waiting on this lane to preempt the queue.
            detach(self._arbiter.take_back())
            return
        self._track_player.stop()

    def _new_player(self) -> RendererPlayer:
        player = RendererPlayer(
            self._config,
            self._registry,
            self._sessions,
            self.state_monitor,
            before_claim=lambda _renderer_id: self._arbiter.acquire(self),
        )
        player.on_interrupted(self._resume_interrupted)
        return player

    async def _resume_interrupted(self, position_ms: int) -> None:
        """The same track on a fresh claim, from where it had reached."""
        await self._retry_current_track_async(position_ms)

    @serialised
    async def switch_renderer(self, renderer_id: Optional[str]) -> None:
        """Move playback to the renderer this selection resolves to.

        The target is claimed before the current renderer is given up, so one
        that is busy or unreachable raises and leaves playback where it was.
        Past that the switch is committed: the old session stops, which clients
        see as a STOPPED, and the track picks up on the new renderer where it
        had reached. An idle queue claims nothing — the selection just takes
        effect at the next play.
        """
        record = (
            self._registry.get(renderer_id) if renderer_id is not None else None
        )
        if record is not None and not record.compatible:
            # Resolution skips it, so accepting the pin would leave playback
            # elsewhere while the client believed it had moved.
            raise RendererUnavailable(
                f"{record.friendly_name or renderer_id} speaks a protocol this "
                f"server does not, and cannot play until it is upgraded"
            )
        target = self._registry.resolve_active(renderer_id)
        # A plugin playing in the queue's place moves instead; the queue has
        # nothing on a renderer to take along.
        if target is not None and await self._arbiter.move_holder(
            target, lambda: self._registry.select(renderer_id)
        ):
            return
        old = self._track_player
        if old.renderer_id is None or old.renderer_id == target:
            self._registry.select(renderer_id)
            return
        if target is None:
            raise RendererUnavailable("no renderer is connected")

        player = self._new_player()
        await player.open(target, announce=False)

        # Commit after the provisional claim, before playback events use the route.
        self._registry.select(renderer_id)

        state = old.get_state()
        resume_at = (
            self._estimated_progress(state)
            if state.state
            in (
                AudioGraphNodeState.PREPARING,
                AudioGraphNodeState.STREAMING,
                AudioGraphNodeState.PAUSED,
            )
            else None
        )
        # Before the stop, not after: the STOPPED reaches _process_state_update
        # while `old` is still current, and a queued stream would restart it.
        self.prepared_tracks.clear()
        self.current_stream_id = None
        self._cancel_prefetch_timer()

        await old.release()
        await old.shutdown()
        self._track_player = player
        await player.announce()
        logger.info("Playback moved to renderer %s", target)
        if resume_at is not None:
            await self._retry_current_track_async(resume_at)

    @serialised
    async def release_renderer(self, renderer_id: str) -> bool:
        """Give this renderer up, stopping playback if the queue is on it.

        For whoever needs the renderer to itself — the speaker test, which
        cannot share the queue's session without its tone being mistaken for
        the current track ending. Stopping is deliberate and reported: clients
        see a STOPPED rather than a queue that quietly stops matching what is
        audible. Returns whether anything was given up.
        """
        if self._track_player.renderer_id != renderer_id:
            return False
        await self._give_up_output()
        logger.info("Released renderer %s", renderer_id)
        return True

    # ------------------------------------------------------------------
    # The queue as the output's default owner (see playback_arbiter)

    @property
    def control(self) -> PlaybackControl:
        return PlaybackControl.queue()

    @property
    def renderer_id(self) -> Optional[str]:
        return self._track_player.renderer_id

    @serialised
    async def preempt(self, reason: RevokeReason) -> None:
        """A plugin takes the output: stop, visibly, and keep the list's place."""
        if await self._give_up_output():
            logger.info("Play queue gave up the output (%s)", reason.value)
        # Reported here rather than when the release's STOPPED comes round,
        # which may be after the new owner took over and the queue went quiet.
        self._arbiter.report(
            self,
            self._to_playback_state(
                StreamState(
                    state=AudioGraphNodeState.STOPPED,
                    timestamp=time.monotonic_ns(),
                )
            ),
        )

    def snapshot(self) -> PlaybackState:
        return self._get_playback_state()

    async def _give_up_output(self) -> bool:
        """Release the renderer; returns whether the queue held one.

        The player is replaced, not reused, so an append still in its sender
        cannot claim the renderer back from whoever takes it next.
        """
        # As in switch_renderer: cleared before the stop, so the STOPPED does
        # not restart the queue on a stream that is already on its way out,
        # and a resolution still in flight does not start one.
        self._resolution.supersede()
        self.prepared_tracks.clear()
        self.current_stream_id = None
        self._cancel_prefetch_timer()
        old = self._track_player
        held = old.renderer_id is not None
        self._track_player = self._new_player()
        await old.release()
        await old.shutdown()
        return held

    @serialised
    async def add(self, tracks: list[TrackInfo], index: Optional[int] = None):
        self._add(tracks, index)

    def _add(self, tracks: list[TrackInfo], index: Optional[int] = None):
        if len(tracks) == 0:
            return

        # Resolve insertion position (default: append)
        insert_at = len(self.track_list) if index is None else index
        # Clamp to valid range
        insert_at = max(0, min(insert_at, len(self.track_list)))

        # An insertion at or before the index a resolution is fetching shifts
        # that target, so the in-flight resolution must be cancelled.
        self._resolution.cancel_if(lambda target: insert_at <= target)

        was_empty = len(self.track_list) == 0
        old_current_track_id = self.current_track_id

        # Insert tracks into the list
        for i, track in enumerate(tracks):
            self.track_list.insert(insert_at + i, track)

        # Shift current_track_id if insertion is before or at it (and queue wasn't empty)
        if not was_empty and insert_at <= self.current_track_id:
            self.current_track_id += len(tracks)

        # Shift all prepared_tracks keys >= insert_at up by len(tracks)
        new_prepared = OrderedDict()
        for idx, stream_info in self.prepared_tracks.items():
            new_prepared[idx + len(tracks) if idx >= insert_at else idx] = stream_info
        self.prepared_tracks = new_prepared

        # Shift unavailable-track indices >= insert_at up by len(tracks)
        self._unavailable_indices = {
            idx + len(tracks) if idx >= insert_at else idx
            for idx in self._unavailable_indices
        }

        self._revalidate_prefetched_next()

        self.event_emitter.dispatch(
            TracksAddedEvent(
                tracks=[
                    track_info
                    for i in range(insert_at, insert_at + len(tracks))
                    if ((track_info := self._get_track_info(i)) is not None)
                ],
                index=insert_at,
            ),
        )

        if was_empty:
            self._notify_track_change()
        elif self.current_track_id != old_current_track_id:
            self._arbiter.report(self, self._get_playback_state())

    @serialised
    async def remove(self, tracks: list[int]):
        prev_track_id = self.current_track_id

        # Removing the resolution target, or any track before it (which shifts
        # it down), invalidates the in-flight resolution.
        self._resolution.cancel_if(lambda target: any(t <= target for t in tracks))

        tracks.sort(reverse=True)
        for track in tracks:
            # Remove the current track's stream
            if track == self.current_track_id and self.current_stream_id is not None:
                self._track_player.remove(self.current_stream_id)
                self.current_stream_id = None
            # Remove prefetched tracks' streams
            if track in self.prepared_tracks:
                _, stream_id = self.prepared_tracks[track]
                self._track_player.remove(stream_id)
                del self.prepared_tracks[track]
                self._cancel_prefetch_timer()

            if track < self.current_track_id:
                self.current_track_id -= 1

            del self.track_list[track]

        # Re-map prepared_tracks keys: each key shifts down by the number of
        # removed indices that were below it.
        new_prepared = OrderedDict()
        for key, value in self.prepared_tracks.items():
            shift = sum(1 for t in tracks if t < key)
            new_prepared[key - shift] = value
        self.prepared_tracks = new_prepared

        # Drop removed indices and shift the rest down to match the new list.
        self._unavailable_indices = {
            idx - sum(1 for t in tracks if t < idx)
            for idx in self._unavailable_indices
            if idx not in tracks
        }

        self.current_track_id = min(self.current_track_id, len(self.track_list) - 1)
        if self.current_track_id < 0:
            self.current_track_id = 0
        self.event_emitter.dispatch(TracksRemovedEvent(indices=tracks))
        if prev_track_id != self.current_track_id or prev_track_id in tracks:
            self._notify_track_change()

    @serialised
    async def list(self, offset: int, limit: int) -> TrackList:
        if offset not in range(0, len(self.track_list)):
            return TrackList(
                offset=offset,
                limit=limit,
                total=len(self.track_list),
                items=[],
            )

        return TrackList(
            offset=offset,
            limit=limit,
            total=len(self.track_list),
            items=[
                track_info
                for i in range(offset, min(offset + limit, len(self.track_list)))
                if ((track_info := self._get_track_info(i)) is not None)
            ],
        )

    @serialised
    async def get_track_info(self, index: int) -> Optional[Track]:
        return self._get_track_info(index)

    def _get_track_info(self, index: int) -> Optional[Track]:
        if index not in range(0, len(self.track_list)):
            return None
        track_info: TrackInfo = self.track_list[index]
        return track_info.metadata

    @serialised
    async def get_playback_state(self) -> PlaybackState:
        if self._arbiter.held_by_plugin:
            return self._arbiter.current_state()
        return self._get_playback_state()

    def _get_playback_state(self) -> PlaybackState:
        return self._to_playback_state(self._track_player.get_state())

    def _to_playback_state(self, stream_state: StreamState) -> PlaybackState:
        """What clients are told, from one renderer state; the single shape
        the REST snapshot and every PlaybackStateChangedEvent are built in."""
        return PlaybackState(
            state=to_state_name(stream_state.state),
            current_track=self._get_track_info(self.current_track_id),
            index=self.current_track_id,
            position=self._estimated_progress(stream_state),
            message=stream_state.error.message if stream_state.error else None,
            audio_info=to_audio_info(stream_state.stream_info),
            mime_type=self.current_format,
            stream_url=self._track_player.stream_uri(self.current_stream_id),
            timestamp_ns=time.monotonic_ns(),
        )

    async def restore_from_state(
        self,
        state: PlayQueueState,
        track_info_retriever: Callable[[EntityId], Awaitable[TrackInfo]],
    ) -> None:
        """Restore playqueue state from a PlayQueueState snapshot.

        Restores the playback mode, track list, and current track index.
        Playback state is set to STOPPED with position 0.

        Args:
            state: The PlayQueueState to restore from
            track_info_retriever: Async callback to retrieve TrackInfo from EntityId
        """
        # Stop any current playback
        was_already_stopped = (
            self._track_player.get_state().state == AudioGraphNodeState.STOPPED
        )
        self._resolution.supersede()
        self._track_player.stop()
        self.current_stream_id = None

        # Clear existing state
        self.track_list.clear()
        self.prepared_tracks.clear()
        self._unavailable_indices.clear()
        self._cancel_prefetch_timer()

        # Pre-restore current_track_id so that _notify_track_change (fired by
        # _add when the queue was empty) emits the correct index straight away,
        # avoiding a spurious event with index=0 followed by the real index.
        target_index = 0
        if state.playback_state and state.playback_state.index is not None:
            target_index = state.playback_state.index
        self.current_track_id = target_index

        # Restore playback mode fields before _add so that expected_next
        # computation inside _add uses the correct repeat settings, and so that
        # any events dispatched during _add reflect the final mode.
        if state.playback_mode:
            self.shuffle = state.playback_mode.shuffle
            self.repeat_single = state.playback_mode.repeat_single
            self.repeat_all = state.playback_mode.repeat_all

        # Restore track list
        if state.track_list:
            track_infos = []
            for track in state.track_list:
                try:
                    track_info = await track_info_retriever(track.id)
                except Exception as e:
                    logger.warning(f"Failed to retrieve track info for {track.id}: {e}")
                    continue

                # The saved snapshot is authoritative for metadata: a module
                # may be unable to re-fetch it on restore (e.g. source-side
                # indexing lag, where the module can still produce a playback
                # source but not the title/artist/album). Use the module's
                # TrackInfo only for playback (source_retriever) and keep the
                # metadata the queue had when it was saved.
                track_infos.append(
                    TrackInfo(
                        id=track.id,
                        source_retriever=track_info.source_retriever,
                        metadata=track,
                    )
                )

            if track_infos:
                self._add(track_infos)

        # Clamp to valid range now that track_list is populated
        if self.track_list:
            self.current_track_id = max(
                0, min(self.current_track_id, len(self.track_list) - 1)
            )
        else:
            self.current_track_id = 0

        # Emit mode event after _add so clients see track state and mode together
        if state.playback_mode:
            self.event_emitter.dispatch(
                PlaybackModeChangedEvent(mode=state.playback_mode)
            )

        # If the player was already stopped before restore, _add's
        # _notify_track_change won't fire (no tracks were loaded), so we need
        # to emit a state event ourselves.
        if was_already_stopped and not self.track_list:
            self._arbiter.report(
                self,
                PlaybackState(
                    current_track=None,
                    index=0,
                    state=PlayerStateEnum.STOPPED,
                    position=0,
                    timestamp_ns=time.monotonic_ns(),
                ),
            )

        logger.info(
            f"Restored state: {len(self.track_list)} tracks, current_track_id={self.current_track_id}"
        )

    @serialised
    async def move(self, from_index: int, to_index: int) -> None:
        if from_index == to_index:
            return
        if from_index not in range(0, len(self.track_list)):
            return
        if to_index not in range(0, len(self.track_list)):
            return

        # A move re-indexes everything between from_index and to_index; if the
        # resolution target falls in that span its index changes, so cancel it.
        lo, hi = min(from_index, to_index), max(from_index, to_index)
        self._resolution.cancel_if(lambda target: lo <= target <= hi)

        # Reorder the track list
        track = self.track_list.pop(from_index)
        self.track_list.insert(to_index, track)

        # Remap current_track_id to follow its track
        old_current_track_id = self.current_track_id
        self.current_track_id = _remap_index(
            self.current_track_id, from_index, to_index
        )

        # Remap all prepared_tracks keys to follow their tracks
        new_prepared = OrderedDict()
        for idx, stream_info in self.prepared_tracks.items():
            new_prepared[_remap_index(idx, from_index, to_index)] = stream_info
        self.prepared_tracks = new_prepared

        # Remap unavailable-track indices to follow their tracks
        self._unavailable_indices = {
            _remap_index(idx, from_index, to_index)
            for idx in self._unavailable_indices
        }

        self._revalidate_prefetched_next()

        self.event_emitter.dispatch(
            TrackMovedEvent(from_index=from_index, to_index=to_index)
        )
        if self.current_track_id != old_current_track_id:
            self._arbiter.report(self, self._get_playback_state())

    @serialised
    async def clear(self):
        self._clear()

    def _clear(self):
        was_already_stopped = (
            self._track_player.get_state().state == AudioGraphNodeState.STOPPED
        )
        self._resolution.supersede()
        self._cancel_prefetch_timer()
        # Null the current stream *before* clear_all(): tearing the graph down
        # makes the switcher emit FINISHED, and the FINISHED handler keys off
        # current_stream_id to tell a teardown from a real track-completion.
        self.current_stream_id = None
        self._track_player.clear_all()
        self.prepared_tracks.clear()
        self._unavailable_indices.clear()
        list_len = len(self.track_list)
        self.track_list = []
        self.current_track_id = 0
        self.event_emitter.dispatch(
            TracksRemovedEvent(indices=[i for i in range(list_len - 1, -1, -1)])
        )
        if was_already_stopped:
            self._arbiter.report(
                self,
                PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    current_track=self._get_track_info(self.current_track_id),
                    index=self.current_track_id,
                    position=0,
                    timestamp_ns=time.monotonic_ns(),
                ),
            )

    def _estimated_progress(self, stream_state: StreamState) -> int:
        return stream_state.position_at(time.monotonic_ns())

    async def _fetch_track_source(self, index):
        """Fetch the stream source for a track as ``(source, reason)`` —
        ``(None, reason)`` if retrieval fails, where ``reason`` is a
        user-presentable cause when the module offered one (a
        SourceUnavailableError) and None otherwise.

        Bounded by SOURCE_RETRIEVAL_TIMEOUT_S so a plugin that ignores its own
        HTTP timeout can never pin the resolution slot indefinitely. Runs
        off-lane, so a slow fetch never blocks the serial executor.
        """
        try:
            track = self.track_list[index]
            source = await asyncio.wait_for(
                track.source_retriever(), timeout=SOURCE_RETRIEVAL_TIMEOUT_S
            )
            if source.sequential:
                logger.warning(
                    "Track source for index %d is sequential; the queue cannot resume it",
                    index,
                )
                return None, "This source plays only through its own plugin"
            return source, None
        except SourceUnavailableError as e:
            logger.warning(
                "Track source for index %d is unavailable: %s", index, e
            )
            return None, str(e)
        except Exception as e:
            logger.warning(
                "Failed to retrieve track link for index %d: %s", index, repr(e)
            )
            return None, None

    async def _resolve_playable(self, start_index, step=1):
        """Find the first playable track from start_index, moving by ``step``.

        Pure I/O: runs off the serial lane and performs no state mutation or
        event dispatch — the caller's commit step records which indices failed
        (``failed`` holds ``(index, reason)`` pairs so they can be flagged with
        a cause) and which one succeeded. ``step`` must be +1
        (forward) or -1 (backward); any other value is normalised so the scan
        visits each track at most once. Returns
        ``(index, track_source, track_ref, failed)`` for the first track that yields
        a source — ``track_ref`` is the TrackInfo whose retriever produced it, so
        the commit can detect a concurrent reindex — or
        ``(None, None, None, failed)`` if none do in that direction.
        Already-prepared tracks are returned from cache.
        """
        n = len(self.track_list)
        failed: list[tuple[int, Optional[str]]] = []
        if n == 0:
            return None, None, None, failed

        step = 1 if step >= 0 else -1

        index = start_index
        for _ in range(n):
            if index < 0 or index >= n:
                if self.repeat_all:
                    index %= n
                else:
                    return None, None, None, failed
            if index in self.prepared_tracks:
                # Already prepared — its source is known good.
                return index, self.prepared_tracks[index][0], self.track_list[index], failed
            # Capture the track ref before awaiting: _fetch_track_source reads the
            # same self.track_list[index] before its own await, so this is the
            # exact track the resolved source belongs to even if the list shifts
            # during the await.
            track_ref = self.track_list[index]
            track_source, fail_reason = await self._fetch_track_source(index)
            if track_source is not None:
                return index, track_source, track_ref, failed
            failed.append((index, fail_reason))
            index += step
        return None, None, None, failed

    def _set_track_unavailable(self, index, unavailable, reason=None):
        """Notify clients of a track availability change (only on real change)."""
        if unavailable:
            if index in self._unavailable_indices:
                return
            self._unavailable_indices.add(index)
        else:
            if index not in self._unavailable_indices:
                return
            self._unavailable_indices.discard(index)
        self.event_emitter.dispatch(
            TrackUnavailableEvent(index=index, unavailable=unavailable, reason=reason)
        )

    async def _retry_current_track_async(self, position_ms: int) -> None:
        """Re-fetch the current track's source off-lane and resume from position_ms.

        Whenever the same track has to be played again on a fresh claim: an
        HTTP stream error, a renderer that came back, a switch to another one.
        The fetch must not run on the interrupt lane (it would stall all state
        updates), so it is delegated to the off-lane resolver. Unlike play/next,
        this targets exactly the current track (single fetch, no skipping) and
        reports failure as a PlaybackErrorEvent.
        """
        index = self.current_track_id
        self._begin_resolution(
            index,
            resolve=lambda: self._fetch_track_source(index),
            next_step=lambda gen, track_source: self._retry_resolved(
                gen, index, track_source, position_ms
            ),
        )

    @serialised
    async def _retry_resolved(self, gen, index, fetch_result, position_ms) -> None:
        if not self._resolution.is_current(gen):
            return
        self._resolution.finish(gen)
        track_source, fail_reason = fetch_result
        if track_source is None:
            self.event_emitter.dispatch(
                PlaybackErrorEvent(
                    message=fail_reason
                    or "Failed to retrieve track source on retry"
                )
            )
            return
        self._apply_retry(index, track_source, position_ms)

    def _apply_retry(
        self, index: int, track_source: TrackSource, position_ms: int
    ) -> None:
        """Re-append the already-resolved current track, starting at position_ms.

        The offset rides the append rather than a seek behind it: a seek can
        only address a stream that is already playing, so one sent this early
        is either dropped or applied before the format is known — which rounds
        it to the start of the track.
        """
        self._cancel_prefetch_timer()

        if self.current_stream_id is not None:
            self._track_player.remove(self.current_stream_id)
            self.current_stream_id = None

        self._clear_prepared_streams()

        self._retry_pending = True
        stream_id = self._new_stream_id()
        self._track_player.append(stream_id, track_source, position_ms)
        self.prepared_tracks[index] = (track_source, stream_id)

    def _request_more_tracks(self):
        if not self.repeat_all and self.current_track_id == len(self.track_list) - 1:
            self.event_emitter.dispatch(RequestMoreTracksEvent())

    def _setup_prefetch_timer(self, state: StreamState):
        self._cancel_prefetch_timer()

        stream_info = state.stream_info
        # Unknown length names no moment to prefetch at; FINISHED ends the track.
        if not stream_info or stream_info.duration_ms is None:
            return

        time_to_prefetch_s = (
            stream_info.duration_ms - state.position - PREFETCH_TIME_MS
        ) / 1000

        if time_to_prefetch_s <= 0:
            self._prefetch_task = asyncio.create_task(self._play_next_track_async())
            return

        logger.info(f"Prefetching next track in {time_to_prefetch_s} seconds")

        self._prefetch_task = asyncio.create_task(
            self._prefetch_timer_async(time_to_prefetch_s)
        )

    async def _prefetch_timer_async(self, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._play_next_track_async()
        except asyncio.CancelledError:
            logger.debug("Prefetch timer cancelled")
            raise

    def _next_index(self) -> int:
        """The index that should play after the current track, honoring the
        repeat modes (repeat_single stays put; repeat_all wraps to 0)."""
        nxt = self.current_track_id
        if not self.repeat_single:
            nxt += 1
        if self.repeat_all and nxt >= len(self.track_list):
            nxt = 0
        return nxt

    def _revalidate_prefetched_next(self) -> None:
        """After a structural change, drop any prefetched stream that is no
        longer the correct next track and re-prefetch the right one."""
        expected_next = self._next_index()
        for idx in list(self.prepared_tracks.keys()):
            if idx == self.current_track_id:
                continue
            if idx != expected_next:
                _, stream_id = self.prepared_tracks.pop(idx)
                self._track_player.remove(stream_id)
                self._cancel_prefetch_timer()
                self._prefetch_task = asyncio.create_task(self._play_next_track_async())

    async def _play_next_track_async(self):
        await self.play_next(self._next_index())

    def _cancel_prefetch_timer(self):
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            self._prefetch_task = None

    def _notify_track_change(self):
        self._arbiter.report(
            self,
            PlaybackState(
                current_track=self._get_track_info(self.current_track_id),
                index=self.current_track_id,
                state=to_state_name(AudioGraphNodeState.STOPPED),
                position=0,
                timestamp_ns=time.monotonic_ns(),
            ),
        )

    @serialised
    async def set_playback_mode(
        self,
        shuffle: Optional[bool],
        repeat_single: Optional[bool],
        repeat_all: Optional[bool],
    ) -> PlaybackMode:
        repeat_single_updated = (
            self.repeat_single != repeat_single if repeat_single is not None else False
        )
        self.shuffle = shuffle if shuffle is not None else self.shuffle
        self.repeat_single = (
            repeat_single if repeat_single is not None else self.repeat_single
        )
        self.repeat_all = repeat_all if repeat_all is not None else self.repeat_all
        if (
            shuffle is not None
            or repeat_all is not None
            or repeat_single is not None
        ):
            self.event_emitter.dispatch(
                PlaybackModeChangedEvent(
                    mode=PlaybackMode(
                        shuffle=self.shuffle,
                        repeat_single=self.repeat_single,
                        repeat_all=self.repeat_all,
                    )
                ),
            )
            if repeat_single_updated:
                for idx in list(self.prepared_tracks.keys()):
                    if idx != self.current_track_id:
                        _, stream_id = self.prepared_tracks.pop(idx)
                        self._track_player.remove(stream_id)
                        self._cancel_prefetch_timer()
                        self._prefetch_task = asyncio.create_task(
                            self._play_next_track_async()
                        )
                        break
        return PlaybackMode(
            shuffle=self.shuffle,
            repeat_single=self.repeat_single,
            repeat_all=self.repeat_all,
        )

    @serialised
    async def get_playback_mode(self) -> PlaybackMode:
        return PlaybackMode(
            shuffle=self.shuffle,
            repeat_single=self.repeat_single,
            repeat_all=self.repeat_all,
        )
