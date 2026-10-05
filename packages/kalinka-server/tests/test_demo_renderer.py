"""The demo output: a renderer that plays nothing but keeps time, driven
through the real session pool with a clock the tests step by hand."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock

import pytest
from kalinka_plugin_sdk import EventEmitter

from kalinka_server.config_model import KalinkaConfig
from kalinka_server.demo_renderer import RENDERER_ID, DemoRenderer
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_config import RendererConfigService
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.renderer_state import StateChange
from kalinka_server.stream_state import to_stream_id
from tests.fake_track_sources import FakeTrackSources, example_track

PLAYING = pb.PLAYBACK_STATE_PLAYING
PAUSED = pb.PLAYBACK_STATE_PAUSED
PREPARING = pb.PLAYBACK_STATE_PREPARING
FINISHED = pb.PLAYBACK_STATE_FINISHED
STOPPED = pb.PLAYBACK_STATE_STOPPED


@dataclass
class _Timer:
    at: int
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class ManualClock:
    now: int = 0
    timers: list[_Timer] = field(default_factory=list)

    def now_ms(self) -> int:
        return self.now

    def unix_ms(self) -> int:
        return 1_700_000_000_000 + self.now

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(self.now + round(delay_s * 1000), callback)
        self.timers.append(timer)
        return timer

    def advance(self, ms: int) -> None:
        """Move time on, firing every timer that falls due on the way."""
        target = self.now + ms
        while due := [t for t in self.pending if t.at <= target]:
            timer = min(due, key=lambda t: t.at)
            self.timers.remove(timer)
            self.now = timer.at
            timer.callback()
        self.now = target

    @property
    def pending(self) -> list[_Timer]:
        return [t for t in self.timers if not t.cancelled]


class Wire:
    """Every state message the output hands the pool, as sent."""

    def __init__(self, pool: SessionPool):
        self.messages: list[tuple[StateChange, Any]] = []
        deliver = pool.handle_state

        def record(renderer_id, *, session_id, change, message):
            self.messages.append((change, message))
            deliver(renderer_id, session_id=session_id, change=change, message=message)

        pool.handle_state = record

    def states(self) -> list[tuple[int, int, str]]:
        """(state, position_ms, source_token) of each playback state."""
        return [
            (m.state, m.position_ms, m.source_token)
            for change, m in self.messages
            if change is StateChange.PLAYBACK
        ]

    def of(self, change: StateChange) -> list[Any]:
        return [m for c, m in self.messages if c is change]

    def clear(self) -> None:
        self.messages.clear()


@dataclass
class Rig:
    renderer: DemoRenderer
    pool: SessionPool
    configs: RendererConfigService
    clock: ManualClock
    wire: Wire
    durations: dict[str, int]


@pytest.fixture
def rig() -> Rig:
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    configs = RendererConfigService(registry)
    clock = ManualClock()
    durations: dict[str, int] = {}
    renderer = DemoRenderer(registry, pool, configs, durations.get, clock)
    renderer.connect()
    return Rig(renderer, pool, configs, clock, Wire(pool), durations)


async def _session(rig: Rig):
    session = await rig.pool.open(RENDERER_ID)
    rig.wire.clear()
    return session


async def test_a_source_plays_for_its_length(rig):
    rig.durations["1"] = 3000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")

    [changed] = rig.wire.of(StateChange.SOURCE)
    assert changed.source_token == "1" and not changed.HasField("previous_source_token")
    assert rig.wire.states() == [(PREPARING, 0, "1"), (PLAYING, 0, "1")]
    assert rig.wire.of(StateChange.PLAYBACK)[-1].duration_ms == 3000

    rig.wire.clear()
    rig.clock.advance(2999)
    assert rig.wire.messages == []
    rig.clock.advance(1)
    assert rig.wire.states() == [(FINISHED, 3000, "1")]


async def test_a_queued_source_takes_over_at_the_boundary(rig):
    rig.durations.update({"1": 1000, "2": 2000})
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await session.enqueue_source("http://x/2", source_token="2")
    rig.wire.clear()

    rig.clock.advance(1000)
    [changed] = rig.wire.of(StateChange.SOURCE)
    assert (changed.source_token, changed.previous_source_token) == ("2", "1")
    assert rig.wire.states() == [(PREPARING, 0, "2"), (PLAYING, 0, "2")]
    assert rig.wire.of(StateChange.PLAYBACK)[-1].duration_ms == 2000

    rig.wire.clear()
    rig.clock.advance(2000)
    assert rig.wire.states() == [(FINISHED, 2000, "2")]


async def test_a_source_starts_where_it_was_told(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1", start_offset_ms=4000)
    assert rig.wire.states()[-1] == (PLAYING, 4000, "1")
    rig.wire.clear()
    rig.clock.advance(1000)
    assert rig.wire.states() == [(FINISHED, 5000, "1")]


async def test_a_pause_stops_the_clock_and_a_resume_starts_it(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    rig.clock.advance(2000)
    rig.wire.clear()

    await session.pause()
    assert rig.wire.states() == [(PAUSED, 2000, "1")]
    rig.clock.advance(60000)
    assert rig.clock.pending == []

    rig.wire.clear()
    await session.resume()
    assert rig.wire.states() == [(PLAYING, 2000, "1")]
    rig.clock.advance(3000)
    assert rig.wire.states()[-1] == (FINISHED, 5000, "1")


async def test_a_seek_rebuffers_and_finishes_from_the_target(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    rig.clock.advance(1000)
    rig.wire.clear()

    await session.seek(4000)
    assert rig.wire.states() == [(PREPARING, 4000, "1"), (PLAYING, 4000, "1")]
    rig.clock.advance(1000)
    assert rig.wire.states()[-1] == (FINISHED, 5000, "1")


async def test_a_seek_while_paused_stays_paused(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await session.pause()
    rig.wire.clear()

    await session.seek(3000)
    assert rig.wire.states() == [(PREPARING, 3000, "1"), (PAUSED, 3000, "1")]
    assert rig.clock.pending == []


async def test_a_source_of_unknown_length_plays_until_it_is_skipped(rig):
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    assert not rig.wire.of(StateChange.PLAYBACK)[-1].HasField("duration_ms")
    rig.clock.advance(10_000_000)
    assert rig.clock.pending == []
    assert rig.wire.states()[-1][0] == PLAYING


async def test_removing_the_current_source_hands_over_or_finishes(rig):
    rig.durations.update({"1": 5000, "2": 5000})
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await session.enqueue_source("http://x/2", source_token="2")
    rig.wire.clear()

    await session.remove_source("1")
    assert rig.wire.of(StateChange.SOURCE)[-1].source_token == "2"
    assert rig.wire.states()[-1] == (PLAYING, 0, "2")

    rig.wire.clear()
    await session.remove_source("2")
    assert rig.wire.states() == [(FINISHED, 0, "2")]
    assert rig.clock.pending == []


async def test_removing_a_queued_source_leaves_the_current_one_playing(rig):
    rig.durations.update({"1": 1000, "2": 1000})
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await session.enqueue_source("http://x/2", source_token="2")
    await session.remove_source("2")
    rig.wire.clear()

    rig.clock.advance(1000)
    assert rig.wire.of(StateChange.SOURCE) == []
    assert rig.wire.states() == [(FINISHED, 1000, "1")]


@pytest.mark.parametrize(
    "command, rest", [("clear_queue", FINISHED), ("stop", STOPPED)]
)
async def test_clearing_or_stopping_ends_playback_and_its_clock(rig, command, rest):
    rig.durations.update({"1": 5000, "2": 5000})
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await session.enqueue_source("http://x/2", source_token="2")
    rig.wire.clear()

    await getattr(session, command)()
    assert rig.wire.states() == [(rest, 0, "")]
    assert rig.clock.pending == []


async def test_closing_the_session_ends_playback_and_its_clock(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await session.close()
    assert rig.clock.pending == []

    reopened = await rig.pool.open(RENDERER_ID)
    assert reopened.snapshot["playback_state"] == "stopped"


async def test_volume_is_echoed_and_a_snapshot_tells_where_playback_is(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.set_volume(70)
    [changed] = rig.wire.of(StateChange.VOLUME)
    assert changed.volume.current == 70
    assert changed.volume.backend == pb.VOLUME_BACKEND_SOFTWARE

    await session.enqueue_source("http://x/1", source_token="1")
    await session.enqueue_source("http://x/2", source_token="2")
    rig.clock.advance(1500)
    await session.request_snapshot()
    snapshot = rig.wire.of(StateChange.SNAPSHOT)[-1]
    assert snapshot.playback_state == PLAYING
    assert snapshot.position_ms == 1500
    assert snapshot.duration_ms == 5000
    assert snapshot.current_source.source_token == "1"
    assert list(snapshot.queued_source_tokens) == ["2"]
    assert snapshot.volume.current == 70


async def test_the_output_answers_for_its_settings(rig):
    assert await rig.configs.get(RENDERER_ID) == {
        "config_version": "demo",
        "sections": [],
    }
    result = await rig.configs.update(RENDERER_ID, {"output.device": "hw:0"})
    [outcome] = result["outcomes"]
    assert outcome["path"] == "output.device"
    assert outcome["applied"] is False
    assert outcome["error"]


async def test_shutdown_calls_off_the_clock_and_is_idempotent(rig):
    rig.durations["1"] = 5000
    session = await _session(rig)
    await session.enqueue_source("http://x/1", source_token="1")
    await rig.renderer.shutdown()
    await rig.renderer.shutdown()
    assert rig.clock.pending == []


def _short_track(track_id: str, duration_s: int):
    return example_track(track_id).model_copy(update={"duration": duration_s})


@pytest.fixture
async def queue_rig():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    clock = ManualClock()
    queue = PlayQueueImpl(
        KalinkaConfig(),
        Mock(spec=EventEmitter),
        registry,
        pool,
        sources=FakeTrackSources(),
    )
    renderer = DemoRenderer(
        registry,
        pool,
        RendererConfigService(registry),
        lambda token: queue.stream_duration_ms(to_stream_id(token)),
        clock,
    )
    renderer.connect()
    await queue.__aenter__()
    yield queue, clock
    await queue.__aexit__(None, None, None)
    await renderer.shutdown()


async def test_the_queue_moves_on_when_a_track_runs_out(queue_rig):
    queue, clock = queue_rig
    # Shorter than the queue's prefetch lead, so the next track is appended
    # as soon as the first is playing.
    await queue.add([_short_track("1", 1), _short_track("2", 2)])
    await queue.play()
    await asyncio.sleep(0.2)
    assert queue.current_track_id == 0
    assert queue.stream_duration_ms(queue.current_stream_id) == 1000
    [(index, (_, prepared))] = queue.prepared_tracks.items()
    assert index == 1
    assert queue.stream_duration_ms(prepared) == 2000

    clock.advance(1000)
    await asyncio.sleep(0.2)
    assert queue.current_track_id == 1
    assert queue.stream_duration_ms(queue.current_stream_id) == 2000


async def test_a_stream_without_a_length_reports_none(queue_rig):
    queue, _clock = queue_rig
    await queue.add([_short_track("1", 0)])
    await queue.play()
    await asyncio.sleep(0.2)
    assert queue.current_stream_id is not None
    assert queue.stream_duration_ms(queue.current_stream_id) is None
    assert queue.stream_duration_ms(queue.current_stream_id + 100) is None
    assert queue.stream_duration_ms(None) is None
