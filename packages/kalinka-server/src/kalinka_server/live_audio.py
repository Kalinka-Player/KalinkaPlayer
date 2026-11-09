"""Core-owned live PCM sources and disposable, single-reader HTTP routes.

Wire v1: a 32-byte little-endian <8sIHHIIII header (magic, rate, channels,
layout, epoch, target ms, maximum ms, reserved), then <QII records (absolute
source frame, frame count, payload bytes) followed by unchanged PCM. HTTP
chunks have no media significance. A route sends its header immediately, but
chooses its first frame only after the renderer reports output readiness.
"""

from __future__ import annotations

import asyncio
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI, HTTPException, Request

from kalinka_plugin_sdk.live_audio import (
    LiveAudioRef,
    LivePolicy,
    PCM_FORMATS,
    PCM_MIME_TYPE,
    PcmFormat,
)
from kalinka_plugin_sdk.live_content import LiveContentError

from . import live_content
from .netutils import server_base_url

HEADER = struct.Struct("<8sIHHIIII")
RECORD = struct.Struct("<QII")
MAGIC = b"KALPCM1\0"
ROUTE_PREFIX = "/live-audio"
MAX_SOURCE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _Block:
    first: int
    pcm: bytes
    arrived: float


class PcmSource:
    def __init__(
        self,
        registry: LiveAudioRegistry,
        owner: str,
        format: PcmFormat,
        max_buffer_ms: int,
    ):
        if not 20 <= max_buffer_ms <= 2000:
            raise ValueError("live retention must be 20..2000 ms")
        self.registry, self.owner = registry, owner
        self.reference = LiveAudioRef(live_source_id=uuid.uuid4().hex)
        self.format = format
        self.max_buffer_ms = max_buffer_ms
        self.closed = False
        self.epoch = 1
        self.frames = 0
        self.blocks: deque[_Block] = deque()
        self.changed = asyncio.Event()
        self._listeners: set[Callable[[], None]] = set()
        self._validate_size(format)

    def _validate_size(self, format: PcmFormat) -> None:
        if (
            format.sample_rate * format.frame_bytes * self.max_buffer_ms // 1000
            > MAX_SOURCE_BYTES
        ):
            raise ValueError("live PCM retention exceeds the memory limit")

    def _wake(self) -> None:
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    def offer(self, pcm: bytes) -> None:
        if self.closed:
            raise RuntimeError("live source is closed")
        width = self.format.frame_bytes
        if len(pcm) % width:
            raise ValueError("live PCM publication must contain complete frames")
        if not pcm:
            return
        # Bound a record to ten milliseconds even if the receiver reads in bursts.
        block_bytes = max(1, self.format.sample_rate // 100) * width
        now = time.monotonic()
        retain = self.format.sample_rate * self.max_buffer_ms // 1000
        # A huge write must never allocate a huge intermediate queue.
        skipped = max(0, len(pcm) // width - retain)
        self.frames += skipped
        for offset in range(skipped * width, len(pcm), block_bytes):
            block = bytes(pcm[offset : offset + block_bytes])
            self.blocks.append(_Block(self.frames, block, now))
            self.frames += len(block) // width
            while self.blocks and self.frames - self.blocks[0].first > retain:
                self.blocks.popleft()
        self._wake()

    def reset(self, format: PcmFormat | None = None) -> None:
        if self.closed:
            raise RuntimeError("live source is closed")
        if format is not None:
            self._validate_size(format)
            self.format = format
        self.epoch = (self.epoch + 1) & 0xFFFFFFFF or 1
        self.frames = 0
        self.blocks.clear()
        self._changed_epoch()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.blocks.clear()
        self.registry.sources.pop(self.reference.live_source_id, None)
        self._changed_epoch()

    def _changed_epoch(self) -> None:
        self._wake()
        for callback in tuple(self._listeners):
            callback()

    def watch(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)


class LiveRoute:
    size = None

    def __init__(self, source: PcmSource, policy: LivePolicy):
        self.id = uuid.uuid4().hex
        self.source, self.policy = source, policy
        self.epoch = source.epoch
        self.format = source.format
        self.created = time.monotonic()
        self.ready = False
        self.closed = False
        self.opened = False
        self.first_frame: int | None = None
        self.cursor: int | None = None
        self.reason: str | None = None
        self._stop_watch = source.watch(self.close)

    def activate(self) -> None:
        if self.closed or self.ready:
            return
        self.ready = True
        # No stale startup PCM: the hardware has finished its settling wait.
        self.cursor = self.source.frames
        self.source._wake()

    def close(self, reason: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.reason = reason
        self._stop_watch()
        self.source._wake()

    def feedback(self, position_ms: int, source_frame: int | None = None) -> bool:
        """False means audio has aged past the route's complete-path budget.

        Positions are relative to the first PCM record, not host clock values.
        The emitter reports the audible position, including its hardware delay.
        """
        if self.closed or self.first_frame is None:
            return not self.closed
        played = (
            source_frame
            if source_frame is not None
            else self.first_frame + position_ms * self.format.sample_rate // 1000
        )
        backlog = (self.source.frames - played) * 1000 / self.format.sample_rate
        if backlog > self.policy.max_buffer_ms:
            self.close("Live audio fell behind; restarting at the live edge")
        return not self.closed

    async def open(self, start: int, end: int | None):
        if start or end is not None:
            raise LiveContentError(409, "live audio cannot seek")
        if self.closed or time.monotonic() - self.created > 30:
            raise LiveContentError(410, "live route expired")
        if self.opened:
            raise LiveContentError(409, "live route already has a reader")
        self.opened = True
        return _Reader(self)


class _Reader:
    def __init__(self, route: LiveRoute):
        self.route = route
        self.pending = HEADER.pack(
            MAGIC,
            route.format.sample_rate,
            route.format.channels,
            PCM_FORMATS[route.format.sample_format][0],
            route.epoch,
            route.policy.target_latency_ms,
            route.policy.max_buffer_ms,
            0,
        )
        self.closed = False

    async def read(self, count: int) -> bytes:
        if count <= 0:
            return b""
        route, source = self.route, self.route.source
        while not self.closed and not route.closed:
            if self.pending:
                result, self.pending = self.pending[:count], self.pending[count:]
                return result
            changed = source.changed
            if source.closed or source.epoch != route.epoch:
                return b""
            if route.ready:
                if source.blocks and route.cursor < source.blocks[0].first:
                    route.close("Live reader exceeded the retained audio window")
                    return b""
                block = next(
                    (b for b in source.blocks if b.first == route.cursor), None
                )
                if block is not None:
                    if (
                        time.monotonic() - block.arrived
                        > route.policy.max_buffer_ms / 1000
                    ):
                        route.close("Live reader reached stale audio")
                        return b""
                    frames = len(block.pcm) // route.format.frame_bytes
                    if route.first_frame is None:
                        route.first_frame = block.first
                    route.cursor += frames
                    self.pending = (
                        RECORD.pack(block.first, frames, len(block.pcm)) + block.pcm
                    )
                    continue
            try:
                await asyncio.wait_for(changed.wait(), 15)
            except asyncio.TimeoutError:
                route.close("Live receiver stopped producing audio")
        return b""

    async def aclose(self) -> None:
        self.closed = True
        self.pending = b""
        self.route.close()


class LiveAudioService:
    def __init__(self, registry: LiveAudioRegistry, owner: str):
        self.registry, self.owner = registry, owner

    def create_pcm(self, format: PcmFormat, *, max_buffer_ms: int = 200) -> PcmSource:
        sources = self.registry.sources
        if (
            len(sources) >= 32
            or sum(s.owner == self.owner for s in sources.values()) >= 4
        ):
            raise RuntimeError("too many open live audio sources")
        source = PcmSource(self.registry, self.owner, format, max_buffer_ms)
        sources[source.reference.live_source_id] = source
        return source


class LiveAudioRegistry:
    def __init__(self):
        self.sources: dict[str, PcmSource] = {}
        self.routes: dict[str, LiveRoute] = {}

    def for_plugin(self, owner: str) -> LiveAudioService:
        return LiveAudioService(self, owner)

    def resolve(self, reference: LiveAudioRef, owner: str | None = None) -> PcmSource:
        source = self.sources.get(reference.live_source_id)
        if (
            source is None
            or source.closed
            or (owner is not None and source.owner != owner)
        ):
            raise ValueError("live source is closed or belongs to another plugin")
        return source

    def bind(self, reference: LiveAudioRef, policy: LivePolicy) -> LiveRoute:
        self.routes = {
            key: route for key, route in self.routes.items() if not route.closed
        }
        source = self.resolve(reference)
        route = LiveRoute(source, policy)
        self.routes[route.id] = route
        return route

    def retire(self, route: LiveRoute) -> None:
        route.close()
        self.routes.pop(route.id, None)

    def close(self) -> None:
        for source in list(self.sources.values()):
            source.close()
        for route in list(self.routes.values()):
            route.close()
        self.routes.clear()


def route_url(server_addr: tuple[str, int], route: LiveRoute) -> str:
    return f"{server_base_url(server_addr)}{ROUTE_PREFIX}/{route.id}"


def register_live_audio_route(app: FastAPI, registry: LiveAudioRegistry) -> None:
    @app.api_route(f"{ROUTE_PREFIX}/{{route_id}}", methods=["GET", "HEAD"])
    async def get_live_audio(route_id: str, request: Request):
        route = registry.routes.get(route_id)
        if route is None or route.closed:
            raise HTTPException(410, "Live audio route expired")
        response = await live_content.serve(route, PCM_MIME_TYPE, request)
        response.headers["Cache-Control"] = "no-store, no-transform"
        response.headers["X-Accel-Buffering"] = "no"
        # Starlette's gzip middleware skips an explicit content encoding.
        response.headers["Content-Encoding"] = "identity"
        return response
