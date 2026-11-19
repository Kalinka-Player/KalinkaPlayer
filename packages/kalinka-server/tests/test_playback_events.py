"""The renderer's signal path and playback events, end to end against a
simulated renderer: what clients see in the playback state, and which events
reach them."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import kalinka_server
import pytest
from kalinka_eventbus import EventBus
from kalinka_plugin_sdk import EventEmitter
from kalinka_plugin_sdk.datamodel import (
    PlaybackMode,
    PlaybackState,
    PlayerStateEnum,
    SignalPath,
    SignalStep,
)
from kalinka_plugin_sdk.events import (
    PlaybackEventReport,
    PlaybackStateChangedEvent,
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playback_arbiter import PlaybackArbiter
from kalinka_server.playback_events import PlaybackEventRelay
from kalinka_server.playback_view import token_to_play_id
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.renderer_test_tone import TonePlayer

from tests.fake_track_sources import FakeTrackSources, example_track
from tests.sim_renderer import (
    DURATION_MS,
    SimRenderer,
    clipping,
    dsd_conversion,
    of_a_newer_kind,
)

SETTLE_S = 0.2


@pytest.fixture
def registry():
    return RendererRegistry(offline_timeout_s=30.0)


@pytest.fixture
def pool(registry):
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    return pool


@pytest.fixture
def renderer(registry, pool):
    sim = SimRenderer(registry, pool)
    sim.connect()
    return sim


@pytest.fixture
def emitter():
    return Mock(spec=EventEmitter)


@pytest.fixture
def arbiter(emitter):
    return PlaybackArbiter(emitter)


@pytest.fixture
def relay(renderer, emitter):
    return PlaybackEventRelay(renderer.registry, renderer.pool, emitter.dispatch)


@pytest.fixture
async def queue(renderer, emitter, arbiter, relay):
    playqueue = PlayQueueImpl(
        KalinkaConfig(),
        emitter,
        renderer.registry,
        renderer.pool,
        sources=FakeTrackSources(),
        arbiter=arbiter,
    )
    await playqueue.__aenter__()
    yield playqueue
    await playqueue.__aexit__(None, None, None)


def _events(emitter) -> list:
    return [call.args[0] for call in emitter.dispatch.call_args_list]


def _reports(emitter) -> list[PlaybackEventReport]:
    return [e for e in _events(emitter) if isinstance(e, PlaybackEventReport)]


def _shown(emitter) -> PlaybackState:
    return [
        e.state for e in _events(emitter) if isinstance(e, PlaybackStateChangedEvent)
    ][-1]


async def _play(queue, renderer, *tracks: str) -> None:
    await queue.add([example_track(t) for t in tracks])
    await queue.play()
    await asyncio.sleep(SETTLE_S)
    assert renderer.current is not None, "expected the queue to be playing"


async def _read_ahead(renderer) -> None:
    renderer.report_position(DURATION_MS - 1000)
    await asyncio.sleep(SETTLE_S)
    assert renderer.queued, "expected the next play to be read ahead"


async def _repeat_one(queue, renderer) -> None:
    await queue.set_playback_mode(None, True, None)
    await _read_ahead(renderer)
    renderer.finish_current()


async def _play_it_again(queue, renderer) -> None:
    await queue.play(0)


async def _next_is_the_same_track(queue, renderer) -> None:
    await queue.next()


async def _restart_it_for_a_setting(queue, renderer) -> None:
    renderer.rebuild()


async def _restart_the_renderer(queue, renderer) -> None:
    await renderer.restart()


async def _retry_a_broken_stream(queue, renderer) -> None:
    renderer.fail_current()


async def _drain(stream) -> list:
    received = []
    while True:
        try:
            received.append(await asyncio.wait_for(stream.__anext__(), SETTLE_S))
        except asyncio.TimeoutError:
            return received


def _bus() -> EventBus:
    return EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )


async def test_clients_are_shown_what_the_renderer_does_to_the_samples(
    queue, renderer, emitter
):
    renderer.signal_path = dsd_conversion()

    await _play(queue, renderer, "1")

    assert _shown(emitter).audio_info.output.signal_path == SignalPath(
        steps=[
            SignalStep(
                kind="dsd_to_pcm",
                name="DSD to PCM",
                summary="DSD64 to 176.4 kHz, 30 kHz filter",
            ),
            SignalStep(kind="dsd_gain", name="DSD gain", summary="+2.8 dB"),
            SignalStep(kind="dither", name="Dither", summary="TPDF at 24 bits"),
        ],
        latency_ms=0.3,
    )


async def test_an_event_about_the_playing_source_reaches_clients_naming_its_play(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1")
    playing = _shown(emitter).play_id

    renderer.report_event(clipping(3, renderer.current))

    assert playing == token_to_play_id(renderer.current)
    assert _reports(emitter) == [
        PlaybackEventReport(
            play_id=playing, kind="clipping", details={"clipped_samples": 3}
        )
    ]


async def test_an_event_about_no_source_reaches_clients_naming_no_play(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1")

    renderer.report_event(clipping(4, None))

    assert [(r.play_id, r.details) for r in _reports(emitter)] == [
        (None, {"clipped_samples": 4})
    ]


async def test_nothing_playing_names_no_play(queue, renderer, emitter):
    await _play(queue, renderer, "1")

    await queue.stop()
    await asyncio.sleep(SETTLE_S)

    assert _shown(emitter).state == PlayerStateEnum.STOPPED
    assert _shown(emitter).play_id is None


@pytest.mark.parametrize(
    "replay",
    [
        _repeat_one,
        _play_it_again,
        _next_is_the_same_track,
        _restart_it_for_a_setting,
        _restart_the_renderer,
        _retry_a_broken_stream,
    ],
)
async def test_a_replay_of_the_same_track_is_a_new_play(
    queue, renderer, emitter, replay
):
    await _play(queue, renderer, "1", "1")
    first = _shown(emitter)
    renderer.report_event(clipping(1200, renderer.current))

    await replay(queue, renderer)
    await asyncio.sleep(SETTLE_S)
    renderer.report_event(clipping(3, renderer.current))

    again = _shown(emitter)
    assert again.state == PlayerStateEnum.PLAYING
    assert again.current_track.id == first.current_track.id
    assert again.play_id == token_to_play_id(renderer.current)
    assert again.play_id != first.play_id
    assert [(r.play_id, r.details) for r in _reports(emitter)] == [
        (first.play_id, {"clipped_samples": 1200}),
        (again.play_id, {"clipped_samples": 3}),
    ]


async def test_an_event_that_overtakes_the_state_of_its_play_names_that_play(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1", "2")
    await _read_ahead(renderer)
    before = _shown(emitter).play_id

    renderer.switch_unreported()
    renderer.report_event(clipping(2, renderer.current))

    assert _shown(emitter).play_id == before
    assert _reports(emitter)[-1].play_id == token_to_play_id(renderer.current)
    assert _reports(emitter)[-1].play_id != before
    await asyncio.sleep(SETTLE_S)
    renderer.report_position(1000)
    await asyncio.sleep(SETTLE_S)
    assert _shown(emitter).play_id == token_to_play_id(renderer.current)


async def test_a_play_moved_to_another_renderer_is_a_new_play(queue, renderer, emitter):
    await _play(queue, renderer, "1")
    first = _shown(emitter).play_id
    other = SimRenderer(renderer.registry, renderer.pool, "other-renderer")
    other.connect()

    await queue.switch_renderer(other.RENDERER_ID)
    await asyncio.sleep(SETTLE_S)
    other.report_event(clipping(3, other.current))

    moved = _shown(emitter)
    assert moved.state == PlayerStateEnum.PLAYING
    assert moved.play_id == token_to_play_id(other.current)
    assert moved.play_id != first
    assert _reports(emitter)[-1].play_id == moved.play_id


def _play_id_in_a_new_server_process(stream_id: int) -> str:
    script = (
        "from kalinka_server.playback_view import to_play_id\n"
        "from kalinka_server.stream_state import AudioGraphNodeState\n"
        f"print(to_play_id(AudioGraphNodeState.STREAMING, {stream_id}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(kalinka_server.__file__).parents[1]))
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def test_a_restarted_server_never_names_a_play_as_the_last_run_did():
    before, after = (_play_id_in_a_new_server_process(17) for _ in range(2))

    assert before.endswith("17") and after.endswith("17")
    assert before != after


async def test_events_about_a_source_that_is_not_playing_are_dropped(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1", "2")
    first = renderer.current

    renderer.report_event(clipping(1, "not-a-source-of-ours"))
    await queue.next()
    await asyncio.sleep(SETTLE_S)
    assert renderer.current != first
    renderer.report_event(clipping(2, first))
    renderer.report_event(clipping(3, renderer.current))

    assert [r.details for r in _reports(emitter)] == [{"clipped_samples": 3}]


async def test_events_from_a_renderer_clients_are_not_shown_are_dropped(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1")
    other = SimRenderer(renderer.registry, renderer.pool, "other-renderer")
    other.connect()
    session = await renderer.pool.open(other.RENDERER_ID)
    await session.set_source("http://192.0.2.10:8000/x.flac", source_token="9")
    assert renderer.registry.serving_id() == renderer.RENDERER_ID

    other.report_event(clipping(5, "9"))
    other.report_event(clipping(6, None))

    assert _reports(emitter) == []
    await session.close()


async def test_a_speaker_test_reports_no_events(queue, renderer, emitter, arbiter):
    tones = TonePlayer(renderer.registry, renderer.pool, arbiter.vacate)
    await tones.play(renderer.RENDERER_ID, "left", "http://192.0.2.10:8000/left.flac")
    assert renderer.session_id is not None
    assert renderer.registry.serving_id() == renderer.RENDERER_ID

    renderer.report_event(clipping(5, renderer.current))
    renderer.report_event(clipping(6, None))

    assert _reports(emitter) == []
    await tones.shutdown()


async def test_an_event_of_a_kind_unknown_here_is_dropped(queue, renderer, emitter):
    await _play(queue, renderer, "1")

    renderer.report_event(of_a_newer_kind(renderer.current))
    renderer.report_event(clipping(7, renderer.current))

    assert [r.kind for r in _reports(emitter)] == ["clipping"]


async def test_an_older_renderer_that_sends_no_events_still_plays(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1", "2")
    await queue.next()
    await asyncio.sleep(SETTLE_S)

    shown = _shown(emitter)
    assert shown.state == PlayerStateEnum.PLAYING
    assert shown.index == 1
    assert shown.audio_info.output.signal_path is None
    assert shown.audio_info.output.lossless_path
    assert _reports(emitter) == []


async def test_playback_events_go_over_the_wire_and_stay_out_of_the_replay(
    registry, pool, renderer
):
    bus = _bus()
    try:
        PlaybackEventRelay(registry, pool, bus.dispatch)
        session = await pool.open(renderer.RENDERER_ID)
        await session.set_source("http://192.0.2.10:8000/7.flac", source_token="7")
        before = bus.get_snapshot()

        async with bus.stream(list(PlayQueueEventType)) as stream:
            await asyncio.wait_for(stream.__anext__(), 1)  # the replay
            renderer.report_event(clipping(1200, "7"))
            event = await asyncio.wait_for(stream.__anext__(), 1)

        assert event.model_dump_json() == (
            '{"event_type":"playback_event","seq":1,'
            f'"play_id":"{token_to_play_id("7")}",'
            '"kind":"clipping","details":{"clipped_samples":1200}}'
        )
        assert bus.get_snapshot().model_dump() == before.model_dump()
        await session.close()
    finally:
        bus.close()


async def test_the_state_and_an_event_name_the_same_play_on_the_wire(
    registry, pool, renderer
):
    bus = _bus()
    PlaybackEventRelay(registry, pool, bus.dispatch)
    playqueue = PlayQueueImpl(
        KalinkaConfig(),
        bus,
        registry,
        pool,
        sources=FakeTrackSources(),
        arbiter=PlaybackArbiter(bus),
    )
    await playqueue.__aenter__()
    try:
        async with bus.stream(list(PlayQueueEventType)) as stream:
            await asyncio.wait_for(stream.__anext__(), 1)  # the replay
            await _play(playqueue, renderer, "1")
            renderer.report_event(clipping(1200, renderer.current))
            received = await _drain(stream)

        state = [
            json.loads(e.model_dump_json())
            for e in received
            if isinstance(e, PlaybackStateChangedEvent)
        ][-1]
        event = next(
            json.loads(e.model_dump_json())
            for e in received
            if isinstance(e, PlaybackEventReport)
        )
        assert state["event_type"] == "state_changed"
        assert state["state"]["state"] == "PLAYING"
        assert state["state"]["play_id"] == token_to_play_id(renderer.current)
        assert event == {
            "event_type": "playback_event",
            "seq": event["seq"],
            "play_id": token_to_play_id(renderer.current),
            "kind": "clipping",
            "details": {"clipped_samples": 1200},
        }
    finally:
        await playqueue.__aexit__(None, None, None)
        bus.close()
