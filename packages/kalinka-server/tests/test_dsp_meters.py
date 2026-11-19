"""The renderer's signal path and DSP meters, end to end against a simulated
renderer: what clients see in the playback state, and which meters reach them."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from kalinka_eventbus import EventBus
from kalinka_plugin_sdk import EventEmitter
from kalinka_plugin_sdk.datamodel import (
    DspMeters,
    PlaybackMode,
    PlaybackState,
    SignalPath,
    SignalStep,
)
from kalinka_plugin_sdk.events import (
    DspMetersChangedEvent,
    PlaybackStateChangedEvent,
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.dsp_meters import DspMetersRelay
from kalinka_server.playback_arbiter import PlaybackArbiter
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.renderer_test_tone import TonePlayer

from tests.fake_track_sources import FakeTrackSources, example_track
from tests.sim_renderer import SimRenderer, dsd_conversion

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
    return DspMetersRelay(renderer.registry, renderer.pool, emitter.dispatch)


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


def _meters(emitter) -> list[DspMeters]:
    return [e.meters for e in _events(emitter) if isinstance(e, DspMetersChangedEvent)]


def _shown(emitter) -> PlaybackState:
    return [
        e.state for e in _events(emitter) if isinstance(e, PlaybackStateChangedEvent)
    ][-1]


async def _play(queue, renderer, *tracks: str) -> None:
    await queue.add([example_track(t) for t in tracks])
    await queue.play()
    await asyncio.sleep(SETTLE_S)
    assert renderer.current is not None, "expected the queue to be playing"


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


async def test_meters_of_the_playing_source_reach_clients(queue, renderer, emitter):
    await _play(queue, renderer, "1")

    renderer.report_meters(clipped_samples=3, true_peak_dbfs=-0.5, load=0.02)

    assert _meters(emitter) == [
        DspMeters(clipped_samples=3, true_peak_dbfs=-0.5, load=0.02)
    ]


async def test_meters_of_a_source_that_is_not_playing_are_dropped(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1", "2")
    first = renderer.current

    renderer.report_meters("not-a-source-of-ours", clipped_samples=1)
    await queue.next()
    await asyncio.sleep(SETTLE_S)
    assert renderer.current != first
    renderer.report_meters(first, clipped_samples=2)
    renderer.report_meters(clipped_samples=3)

    assert [m.clipped_samples for m in _meters(emitter)] == [3]


async def test_meters_from_a_renderer_clients_are_not_shown_are_dropped(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1")
    other = SimRenderer(renderer.registry, renderer.pool, "other-renderer")
    other.connect()
    session = await renderer.pool.open(other.RENDERER_ID)
    await session.set_source("http://192.0.2.10:8000/x.flac", source_token="9")
    assert renderer.registry.serving_id() == renderer.RENDERER_ID

    other.report_meters(clipped_samples=5)

    assert _meters(emitter) == []
    await session.close()


async def test_a_speaker_test_reports_no_meters(queue, renderer, emitter, arbiter):
    tones = TonePlayer(renderer.registry, renderer.pool, arbiter.vacate)
    await tones.play(renderer.RENDERER_ID, "left", "http://192.0.2.10:8000/left.flac")
    assert renderer.session_id is not None
    assert renderer.registry.serving_id() == renderer.RENDERER_ID

    renderer.report_meters(clipped_samples=5)

    assert _meters(emitter) == []
    await tones.shutdown()


async def test_an_older_renderer_shows_no_signal_path_and_sends_no_meters(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1")

    output = _shown(emitter).audio_info.output
    assert output.signal_path is None
    assert output.lossless_path
    assert _meters(emitter) == []


async def test_meters_go_over_the_wire_and_stay_out_of_the_replay(
    registry, pool, renderer
):
    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    try:
        DspMetersRelay(registry, pool, bus.dispatch)
        session = await pool.open(renderer.RENDERER_ID)
        await session.set_source("http://192.0.2.10:8000/7.flac", source_token="7")
        before = bus.get_snapshot()

        async with bus.stream([PlayQueueEventType.DspMetersChanged]) as stream:
            await asyncio.wait_for(stream.__anext__(), 1)  # the replay
            renderer.report_meters(clipped_samples=12, true_peak_dbfs=-0.4, load=0.012)
            event = await asyncio.wait_for(stream.__anext__(), 1)

        assert json.loads(event.model_dump_json()) == {
            "event_type": "dsp_meters_changed",
            "seq": 1,
            "meters": {"clipped_samples": 12, "true_peak_dbfs": -0.4, "load": 0.012},
        }
        assert bus.get_snapshot().model_dump() == before.model_dump()
        await session.close()
    finally:
        bus.close()
