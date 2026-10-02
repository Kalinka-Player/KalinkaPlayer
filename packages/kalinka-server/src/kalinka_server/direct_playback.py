"""The server side of the SDK's DirectPlayback: a plugin plays on the renderer.

Each hold drives the renderer through its own RendererPlayer, the play
queue's, so it gets the same claim, content-URL, reconnect and idle-release
handling; the arbiter decides who may claim. The plugin names its playback
with a Track, which clients are shown in place of the queue's.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.api import ReplayEvent
from kalinka_plugin_sdk.datamodel import (
    DeviceVolume,
    PlaybackControl,
    PlaybackState,
    PlayerStateEnum,
    Track,
)
from kalinka_plugin_sdk.direct_playback import (
    DirectPlaybackListener,
    HoldEnded,
    OutputUnavailable,
    RevokeReason,
    TransportRequest,
)
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
    VolumeChangedEvent,
)
from kalinka_plugin_sdk.inputmodule import TrackSource

from .config_model import KalinkaConfig
from .module_timeout import PLUGIN_CALL_TIMEOUT_S
from .output_device_router import OutputDeviceRouter
from .playback_arbiter import PlaybackArbiter
from .playback_view import to_audio_info, to_state_name
from .renderer_player import RendererPlayer
from .renderer_registry import RendererRegistry, RendererUnavailable
from .renderer_sessions import (
    CloseReason,
    RendererBusy,
    SessionOpenFailed,
    SessionPool,
)
from .stream_state import AudioGraphNodeState, StateMonitor, StreamState

logger = logging.getLogger(__name__.split(".")[-1])

DeviceEvents = EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent]


class _ListenerBridge:
    """Calls into the plugin, in order and one at a time, each within budget.

    A slow or failing plugin costs its own deliveries, never the arbiter's
    lock or the play queue's lane.
    """

    def __init__(self, listener: DirectPlaybackListener, label: str):
        self._listener = listener
        self._label = label
        self._calls: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.get_running_loop().create_task(self._deliver())

    def send(self, method: str, *args: Any) -> None:
        self._calls.put_nowait((method, args))

    def close(self) -> None:
        """Deliver what is queued, then stop."""
        self._calls.put_nowait(None)

    async def _deliver(self) -> None:
        while True:
            call = await self._calls.get()
            if call is None:
                return
            method, args = call
            try:
                result = getattr(self._listener, method)(*args)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, PLUGIN_CALL_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("%s: %s took too long", self._label, method)
            except Exception:
                logger.exception("%s: %s failed", self._label, method)


_PLAYING_STATES = (AudioGraphNodeState.PREPARING, AudioGraphNodeState.STREAMING)
# A source that was playing, or paused partway, when the renderer changed.
_MOVING_STATES = (*_PLAYING_STATES, AudioGraphNodeState.PAUSED)


@dataclass(frozen=True)
class _QueuedSource:
    """One successor awaiting the renderer's source-change event."""

    stream_id: int
    source: TrackSource
    track: Track


class HolderSession:
    """One plugin's hold on the output (SDK DirectPlaybackSession).

    Also the arbiter's OutputOwner for as long as the hold lasts. Once
    ended — released, revoked, or its session lost — it never claims again.
    """

    def __init__(
        self,
        plugin_id: str,
        title: str,
        listener: DirectPlaybackListener,
        *,
        config: KalinkaConfig,
        registry: RendererRegistry,
        pool: SessionPool,
        arbiter: PlaybackArbiter,
        device_router: OutputDeviceRouter,
        device_events: DeviceEvents,
    ):
        self._plugin_id = plugin_id
        self._control = PlaybackControl.exclusive(plugin_id, title)
        self._arbiter = arbiter
        self._router = device_router
        self._device_events = device_events
        self._config = config
        self._registry = registry
        self._pool = pool
        self._bridge = _ListenerBridge(listener, plugin_id)
        self._monitor = StateMonitor()
        self._player = self._new_player()
        # Held while the player is swapped for another renderer's, and while
        # the hold's teardown stops it, so neither sees half the other.
        self._swap = asyncio.Lock()
        self._track: Optional[Track] = None
        self._source: Optional[TrackSource] = None
        self._queued: Optional[_QueuedSource] = None
        self._stream_id: Optional[int] = None
        self._next_stream_id = 0
        self._state = PlaybackState(
            state=PlayerStateEnum.STOPPED, timestamp_ns=time.monotonic_ns()
        )
        self._ended = False
        self._teardown: Optional[asyncio.Future] = None
        self._consumer = asyncio.get_running_loop().create_task(self._consume())
        self._volume_watch = asyncio.get_running_loop().create_task(self._watch_volume())

    # ------------------------------------------------------------------
    # DirectPlaybackSession

    @property
    def active(self) -> bool:
        return not self._ended

    @property
    def renderer_id(self) -> Optional[str]:
        return self._player.renderer_id

    @property
    def state(self) -> PlaybackState:
        return self._state

    async def open(self, renderer_id: str) -> None:
        """Claim the renderer now, so a failure reaches the plugin's acquire."""
        await self._player.open(renderer_id, announce=True)

    async def play(
        self, source: TrackSource, track: Track, *, start_offset_ms: int = 0
    ) -> None:
        self._require_active()
        if source.sequential and start_offset_ms:
            raise ValueError("a sequential source starts at its first byte")
        self._clear_next()
        self._start(source, track, start_offset_ms)

    async def set_next(
        self, source: Optional[TrackSource], track: Optional[Track] = None
    ) -> None:
        self._require_active()
        if source is not None:
            if track is None:
                raise ValueError("a queued source needs track metadata")
            if source.sequential:
                raise ValueError("a sequential source cannot be queued ahead")
            if self._source is None:
                raise ValueError("play a source before setting its successor")
        self._clear_next()
        if source is not None:
            stream_id = self._next_stream_id
            self._next_stream_id += 1
            self._queued = _QueuedSource(stream_id, source, track)
            self._player.append(stream_id, source, 0)

    def _clear_next(self) -> None:
        queued, self._queued = self._queued, None
        if queued is not None:
            self._player.remove(queued.stream_id)

    async def pause(self) -> None:
        self._require_active()
        self._player.pause()

    async def resume(self) -> None:
        self._require_active()
        self._player.resume()

    async def stop(self) -> None:
        self._require_active()
        self._clear_next()
        stream_id, self._stream_id = self._stream_id, None
        self._source = None
        if stream_id is not None:
            self._player.remove(stream_id)
        self._publish(
            StreamState(
                state=AudioGraphNodeState.STOPPED,
                timestamp=time.monotonic_ns(),
            )
        )

    async def seek(self, position_ms: int) -> None:
        self._require_active()
        if self._source is not None and self._source.sequential:
            raise ValueError("a sequential source cannot seek; play a new one")
        self._player.seek(position_ms)

    async def set_volume(self, percent: int) -> None:
        self._require_active()
        device = self._router.current()
        if device is None:
            raise OutputUnavailable("nothing controls this renderer's volume")
        # A device counts in its own steps, up to its own maximum.
        full = (await device.get_volume()).max_volume or 100
        await device.set_volume(round(percent * full / 100))

    async def get_volume(self) -> Optional[DeviceVolume]:
        device = self._router.current()
        return await device.get_volume() if device is not None else None

    async def release(self) -> None:
        if await self._end(None):
            await self._arbiter.release(self)

    # ------------------------------------------------------------------
    # OutputOwner

    @property
    def control(self) -> PlaybackControl:
        return self._control

    async def preempt(self, reason: RevokeReason) -> None:
        await self._end(reason)

    def snapshot(self) -> Optional[PlaybackState]:
        return self._state

    def handle_command(self, request: TransportRequest) -> bool:
        if self._ended:
            return False
        self._bridge.send("on_command", request)
        return True

    async def move_to(self, renderer_id: str, commit: Callable[[], None]) -> None:
        """Carry on on another renderer, from where playback has reached.

        The stream is the one already playing, at the same address; the
        target is claimed before the current renderer is given up. A paused
        track stays paused.
        """
        if self._source is not None and self._source.sequential and renderer_id != self.renderer_id:
            await self._on_session_lost(CloseReason.RENDERER_LOST)
            commit()
            return
        async with self._swap:
            old = self._player
            if self._ended or old.renderer_id in (None, renderer_id):
                commit()
                return
            player = self._new_player()
            try:
                await player.open(renderer_id, announce=False)
            except HoldEnded:
                await player.shutdown()
                commit()
                return
            except BaseException:
                await player.shutdown()
                raise
            commit()
            reached = old.get_state()
            queued, self._queued = self._queued, None
            self._player = player
            # Not release(): its STOPPED would reach clients and the plugin
            # as the playback ending, when it is only moving.
            await old.shutdown()
            await player.announce()
            if reached.state in _MOVING_STATES and self._source is not None:
                self._start(
                    self._source,
                    self._track,
                    reached.position_at(time.monotonic_ns()),
                )
                if queued is not None:
                    await self.set_next(queued.source, queued.track)
                if reached.state is AudioGraphNodeState.PAUSED:
                    self._player.pause()
                logger.info("%s moved to renderer %s", self._plugin_id, renderer_id)
                return
        await self._on_session_lost(None)

    # ------------------------------------------------------------------

    def _new_player(self) -> RendererPlayer:
        player = RendererPlayer(
            self._config,
            self._registry,
            self._pool,
            self._monitor,
            before_claim=self._before_claim,
        )
        player.on_interrupted(self._replay)
        player.on_session_lost(self._on_session_lost)
        return player

    def _start(self, source: TrackSource, track: Optional[Track], offset_ms: int) -> None:
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        previous = self._stream_id
        self._stream_id, self._source, self._track = stream_id, source, track
        # The queue's replace: the new stream queues behind the old, whose
        # removal hands over to it.
        self._player.append(stream_id, source, offset_ms)
        if previous is not None:
            self._player.remove(previous)
        self._publish(
            StreamState(
                state=AudioGraphNodeState.PREPARING,
                position=offset_ms,
                timestamp=time.monotonic_ns(),
            )
        )

    async def _before_claim(self, _renderer_id: str) -> None:
        if self._ended:
            raise HoldEnded("the hold on the output has ended")
        await self._arbiter.acquire(self)

    async def _replay(self, position_ms: int) -> None:
        """The renderer came back mid-track: the same stream from where it was."""
        if self._ended or self._source is None or self._track is None:
            return
        if self._source.sequential:
            await self._on_session_lost(CloseReason.RENDERER_LOST)
            return
        queued = self._queued
        await self.play(self._source, self._track, start_offset_ms=position_ms)
        if queued is not None:
            await self.set_next(queued.source, queued.track)

    async def _on_session_lost(self, reason: Optional[CloseReason]) -> None:
        cause = RevokeReason.IDLE if reason is None else RevokeReason.OUTPUT_LOST
        if await self._end(cause):
            await self._arbiter.release(self)

    async def _end(self, reason: Optional[RevokeReason]) -> bool:
        """End the hold once; False when it had already ended."""
        if self._ended:
            if self._teardown is not None:
                await asyncio.shield(self._teardown)
            return False
        self._ended = True
        self._queued = None
        self._volume_watch.cancel()
        self._teardown = asyncio.ensure_future(self._stop_player())
        await asyncio.shield(self._teardown)
        if reason is not None:
            logger.info("%s lost the output (%s)", self._plugin_id, reason.value)
            self._bridge.send("on_revoked", reason)
        self._bridge.close()
        return True

    async def _stop_player(self) -> None:
        async with self._swap:
            await self._player.release()
            await self._player.shutdown()
        self._monitor.stop()

    async def _consume(self) -> None:
        async for state in self._monitor:
            if self._ended:
                continue
            queued = self._queued
            if queued is not None and state.stream_id == queued.stream_id:
                previous = self._stream_id
                self._stream_id = queued.stream_id
                self._source, self._track = queued.source, queued.track
                self._queued = None
                if previous is not None:
                    self._player.remove(previous)
                self._bridge.send("on_next_started", queued.track)
            if state.stream_id is not None and state.stream_id != self._stream_id:
                continue
            if state.state is AudioGraphNodeState.SOURCE_CHANGED:
                continue
            if state.state is AudioGraphNodeState.FINISHED:
                if self._source is None:
                    continue
                if self._queued is not None:
                    continue
                # The plugin decides whether to play, stop, or release.
                self._bridge.send("on_finished")
                continue
            self._publish(state)

    async def _watch_volume(self) -> None:
        """Tell the plugin the output's volume: now, and at every change.

        The bus carries only the device in charge of the active renderer's
        volume, which is the one the hold plays through.
        """
        async with self._device_events.stream([ExtDeviceEventType.VolumeChanged]) as events:
            async for event in events:
                if isinstance(event, ReplayEvent):
                    self._bridge.send("on_volume", event.state.volume)
                elif isinstance(event, VolumeChangedEvent):
                    self._bridge.send("on_volume", event.volume)

    def _publish(self, state: StreamState) -> None:
        # Timestamped control points let plugins advance their own clocks,
        # just like the play queue. Quiet playback needs no renderer polling.
        self._state = PlaybackState(
            state=to_state_name(state.state),
            current_track=self._track,
            index=None,
            position=state.position_at(time.monotonic_ns()) + (
                self._source.timeline_offset_ms if self._source else 0
            ),
            message=state.error.message if state.error else None,
            audio_info=to_audio_info(state.stream_info),
            mime_type=self._source.format if self._source else None,
            stream_url=self._player.stream_uri(self._stream_id),
            timestamp_ns=time.monotonic_ns(),
        )
        self._arbiter.report(self, self._state)
        self._bridge.send("on_state", self._state)

    def _require_active(self) -> None:
        if self._ended:
            raise HoldEnded("the hold on the output has ended")


class DirectPlaybackService:
    """SDK DirectPlayback for one plugin; the server binds its plugin id."""

    def __init__(
        self,
        plugin_id: str,
        *,
        config: KalinkaConfig,
        registry: RendererRegistry,
        pool: SessionPool,
        arbiter: PlaybackArbiter,
        device_router: Callable[[], Optional[OutputDeviceRouter]],
        device_events: DeviceEvents,
    ):
        self._plugin_id = plugin_id
        self._config = config
        self._registry = registry
        self._pool = pool
        self._arbiter = arbiter
        self._device_router = device_router
        self._device_events = device_events
        self._session: Optional[HolderSession] = None

    async def acquire(
        self, title: str, listener: DirectPlaybackListener
    ) -> HolderSession:
        if self._session is not None:
            await self._session.release()
            self._session = None
        router = self._device_router()
        if router is None or self._registry.active_id() is None:
            raise OutputUnavailable("no renderer is connected")
        session = HolderSession(
            self._plugin_id,
            title,
            listener,
            config=self._config,
            registry=self._registry,
            pool=self._pool,
            arbiter=self._arbiter,
            device_router=router,
            device_events=self._device_events,
        )
        await self._arbiter.acquire(session)
        # Resolved after the queue let go: its claim no longer pins the choice.
        renderer_id = self._registry.active_id()
        try:
            if renderer_id is None:
                raise RendererUnavailable("no renderer is connected")
            await session.open(renderer_id)
        except (
            HoldEnded,
            RendererUnavailable,
            RendererBusy,
            SessionOpenFailed,
            asyncio.TimeoutError,
        ) as exc:
            await session.release()
            raise OutputUnavailable(str(exc) or "the renderer did not answer") from None
        self._session = session
        logger.info("%s took renderer %s", self._plugin_id, renderer_id)
        return session
