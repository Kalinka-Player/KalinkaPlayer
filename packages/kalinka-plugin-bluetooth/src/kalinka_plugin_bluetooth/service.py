"""One Bluetooth sender owns an input session; Core chooses its output."""

from __future__ import annotations

import asyncio
import logging
import time

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    EntityId,
    EntityType,
    PlayerStateEnum,
    Track,
)
from kalinka_plugin_sdk.direct_playback import (
    HoldEnded,
    OutputUnavailable,
    TransportKind,
)
from kalinka_plugin_sdk.live_audio import LivePolicy

from .backend import Endpoint

logger = logging.getLogger(__name__)


class _Listener:
    def __init__(self, service, generation, token):
        self.service, self.generation, self.token = service, generation, token

    def _current(self):
        return (
            self.generation == self.service.generation
            and not self.service.closed
            and self.token is self.service.listener_token
        )

    def _schedule(self, call, *args):
        async def current():
            if self._current():
                await call(*args, self.generation)

        self.service.spawn(current())

    def on_state(self, state):
        if self._current():
            self.service.playback_state = state
            if state.state == PlayerStateEnum.ERROR:
                self.service.output_error = state.message
            elif state.state == PlayerStateEnum.PLAYING:
                self.service.output_error = None

    def on_revoked(self, reason):
        if self._current():
            service = self.service
            service.hold = None
            service.input_idle = False
            service.blocked = True
            service.partial.clear()
            if service.source is not None:
                service.source.reset()
            self._schedule(service.remote, "Pause")

    def on_command(self, request):
        if self._current():
            self._schedule(self.service.command, request)

    def on_volume(self, volume):
        if self._current() and volume.supported and volume.max_volume:
            percent = round(volume.current_volume * 100 / volume.max_volume)
            self._schedule(self.service.output_volume, percent)

    def on_finished(self):
        pass

    def on_next_started(self, track):
        pass


class BluetoothService:
    def __init__(self, live_audio, direct, backend, *, policy=LivePolicy()):
        self.live_audio, self.direct, self.backend = live_audio, direct, backend
        self.policy = policy
        self.endpoint: Endpoint | None = None
        self.source = None
        self.hold = None
        self.listener_token = None
        self.partial = bytearray()
        self.blocked = False
        self.paused = False
        self.input_idle = False
        self.closed = False
        self.generation = 0
        self.claiming = None
        self.tasks: set[asyncio.Task] = set()
        self.last_claim = 0.0
        self.last_audio = 0.0
        self.metadata = {}
        self.playback_state = None
        self.output_error = None
        self._volume = None

    @property
    def status(self):
        if self.endpoint is None:
            return "Waiting for a paired phone"
        if self.blocked:
            return f"Connected to {self.endpoint.alias}; another input owns playback"
        if self.output_error:
            return self.output_error
        if self.paused or self.input_idle or not self.endpoint.running:
            return f"Connected to {self.endpoint.alias}; paused"
        if self.hold is None:
            return f"Receiving from {self.endpoint.alias}; waiting for output"
        return f"Bluetooth · {self.endpoint.alias} · {self.endpoint.codec}"

    def spawn(self, coroutine):
        if self.closed:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.warning("Bluetooth control failed: %s", done.exception())

        task.add_done_callback(finished)
        return task

    async def connected(self, endpoint: Endpoint):
        previous = self.endpoint
        if previous is not None and previous.identity == endpoint.identity:
            self.endpoint = endpoint
            if previous.format != endpoint.format:
                self.partial.clear()
                self.source.reset(endpoint.format)
            if not previous.running and endpoint.running:
                # A new sender play gesture may take ownership after a takeover.
                was_paused = self.paused
                self.blocked = self.paused = False
                self.last_claim = 0
                if was_paused:
                    # Kalinka's pause discarded the route, but kept the hold.
                    # Resume it without echoing Play back to the sender.
                    self.partial.clear()
                    self.source.reset()
                    self.input_idle = False
                    if self.hold is not None and self.hold.active:
                        await self.hold.resume()
            return
        await self.disconnected()
        self.endpoint = endpoint
        self.source = self.live_audio.create_pcm(endpoint.format)
        self.last_claim = 0
        self.blocked = self.paused = False
        self.output_error = None

    def audio(self, pcm: bytes):
        """Called by the FD drain task; never waits for D-Bus or a renderer."""
        if self.endpoint is None or self.closed or not pcm:
            return
        self.last_audio = time.monotonic()
        if self.blocked or self.paused:
            self.partial.clear()
            return
        self.partial.extend(pcm)
        width = self.endpoint.format.frame_bytes
        complete = len(self.partial) // width * width
        if complete:
            self.source.offer(bytes(self.partial[:complete]))
            del self.partial[:complete]
            if self.input_idle:
                # Resume from PCM even if the sender never updates AVRCP or
                # Running. Keep the FD drain free of output/D-Bus awaits.
                self.backend.changed.set()
        if (
            self.hold is None
            and (self.claiming is None or self.claiming.done())
            and time.monotonic() - self.last_claim >= 2
        ):
            self.claiming = self.spawn(self._claim())

    async def _claim(self):
        self.last_claim = time.monotonic()
        source, generation = self.source, self.generation
        hold = None
        self.listener_token = token = object()
        try:
            hold = await self.direct.acquire(
                "Bluetooth", _Listener(self, generation, token), seekable=False
            )
            if (
                self.closed
                or generation != self.generation
                or self.blocked
                or source is not self.source
            ):
                return
            await hold.play_live(source, self.track(), policy=self.policy)
            self.hold = hold
            self.output_error = None
            hold = None
        except (OutputUnavailable, HoldEnded, ValueError) as error:
            self.output_error = str(error)
        finally:
            if hold is not None:
                await hold.release()

    def track(self):
        address = self.endpoint.address if self.endpoint else "input"
        entity = EntityId(
            id=address.replace(":", ""), source="bluetooth", type=EntityType.TRACK
        )
        title = self.metadata.get("Title") or "Bluetooth audio"
        artist = self.metadata.get("Artist")
        album = self.metadata.get("Album")
        return Track(
            id=entity,
            title=title,
            duration=max(0, int(self.metadata.get("Duration", 0))) // 1000,
            album=Album(
                id=EntityId(id="album", source="bluetooth", type=EntityType.ALBUM),
                title=album or (self.endpoint.alias if self.endpoint else "Bluetooth"),
            ),
            performer=(
                Artist(
                    id=EntityId(
                        id="artist", source="bluetooth", type=EntityType.ARTIST
                    ),
                    name=artist,
                )
                if artist
                else None
            ),
        )

    async def update_metadata(self, metadata: dict):
        if metadata == self.metadata:
            return
        self.metadata = metadata
        hold = self.hold
        if hold is not None and hold.active:
            try:
                await hold.show(self.track(), 0)
            except (HoldEnded, ValueError):
                pass

    async def update_activity(self, sender_status: str | None):
        """An idle sender is paused; an expected audio/output stall is buffering."""
        hold, endpoint = self.hold, self.endpoint
        if (
            endpoint is None
            or hold is None
            or not hold.active
            or self.paused
            or self.blocked
            or self.output_error
        ):
            return
        # Allow buffered tail audio and brief track/transport transitions to
        # drain. Stale AVRCP status must never silence PCM from another app.
        idle = time.monotonic() - self.last_audio > 1 and (
            not endpoint.running or sender_status in ("paused", "stopped", "error")
        )
        if idle == self.input_idle:
            return
        self.input_idle = idle
        try:
            if idle:
                self.partial.clear()
                self.source.reset()
                await hold.pause()
            else:
                await hold.resume()
        except HoldEnded:
            pass  # A queue takeover or disconnect can end the hold meanwhile.

    async def remote(self, method, generation=None):
        if generation is not None and generation != self.generation:
            return
        endpoint = self.endpoint
        if endpoint is not None:
            await self.backend.command(endpoint, method)

    async def command(self, request, generation=None):
        if self.closed or (generation is not None and generation != self.generation):
            return
        generation = self.generation
        if request.kind is TransportKind.PAUSE:
            self.paused = True
            self.partial.clear()
            if self.hold and self.hold.active:
                await self.hold.pause()
            await self.remote("Pause", generation)
        elif request.kind is TransportKind.RESUME:
            self.paused = self.blocked = self.input_idle = False
            if self.source:
                self.source.reset()
            if self.hold and self.hold.active:
                await self.hold.resume()
            await self.remote("Play", generation)
        elif request.kind in (TransportKind.NEXT, TransportKind.PREV):
            await self.remote(
                "Next" if request.kind is TransportKind.NEXT else "Previous", generation
            )

    async def input_volume(self, percent):
        if percent == self._volume:
            return
        self._volume = percent
        if self.hold is not None and self.hold.active:
            try:
                await self.hold.set_volume(percent)
            except OutputUnavailable:
                pass  # Fixed output has no adjustable volume authority.

    async def output_volume(self, percent, generation=None):
        if self.closed or (generation is not None and generation != self.generation):
            return
        if self.endpoint is not None and percent != self._volume:
            self._volume = percent
            await self.backend.set_volume(self.endpoint, percent)

    async def disconnected(self):
        self.generation += 1
        self.listener_token = None
        self.partial.clear()
        self.endpoint, self.metadata, self._volume = None, {}, None
        self.input_idle = False
        self.last_audio = 0.0
        claim, self.claiming = self.claiming, None
        source, self.source = self.source, None
        hold, self.hold = self.hold, None
        if source is not None:
            source.close()
        try:
            if claim is not None and not claim.done():
                claim.cancel()
                await asyncio.gather(claim, return_exceptions=True)
        finally:
            if hold is not None:
                await hold.release()

    async def close(self):
        self.closed = True
        try:
            await self.disconnected()
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()
