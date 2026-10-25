"""RendererPlayer when a renderer setting stops the track, as a new output
device does: the owner is asked to play it again on the session it holds,
instead of being told playback stopped."""

import asyncio
import time

import pytest

from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackSource
from kalinka_server import renderer_player
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.renderer_player import RendererPlayer
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.stream_state import AudioGraphNodeState, StreamState

from tests.sim_renderer import SimRenderer

SETTLE_S = 0.05


class Published:
    """The monitor's push, which is all a RendererPlayer asks of one."""

    def __init__(self):
        self.states: list[StreamState] = []

    def push(self, state: StreamState) -> None:
        self.states.append(state)

    def since(self, mark: int) -> list[AudioGraphNodeState]:
        return [state.state for state in self.states[mark:]]


@pytest.fixture
def renderer():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    sim = SimRenderer(registry, pool)
    sim.connect()
    return sim


@pytest.fixture
def published():
    return Published()


@pytest.fixture
async def player(renderer, published):
    player = RendererPlayer(
        KalinkaConfig(), renderer.registry, renderer.pool, published
    )
    yield player
    await player.shutdown()


@pytest.fixture
def resumed(player) -> list[int]:
    positions: list[int] = []
    player.on_interrupted(positions.append)
    return positions


def _source() -> TrackSource:
    return TrackSource(
        source=DirectUrl(url="http://example/1.flac"), format="audio/flac"
    )


async def _playing_at(player, renderer, position_ms: int) -> None:
    player.append(1, _source())
    await asyncio.sleep(SETTLE_S)
    renderer.report_position(position_ms)
    await asyncio.sleep(SETTLE_S)
    assert player.get_state().state is AudioGraphNodeState.STREAMING


async def test_the_owner_is_asked_to_resume_where_the_track_had_reached(
    player, renderer, resumed
):
    await _playing_at(player, renderer, 30000)
    session = renderer.session_id

    renderer.rebuild()
    await asyncio.sleep(SETTLE_S)

    assert len(resumed) == 1
    assert 30000 <= resumed[0] < 31000
    assert renderer.session_id == session, "the session is kept, not reclaimed"


async def test_the_stop_is_not_published(player, renderer, published, resumed):
    await _playing_at(player, renderer, 30000)
    mark = len(published.states)

    renderer.rebuild()
    await asyncio.sleep(SETTLE_S)

    assert published.since(mark) == []


async def test_the_track_holds_still_until_it_plays_again(
    player, renderer, resumed
):
    """The time it spends silent must not be counted as playing time."""
    await _playing_at(player, renderer, 30000)

    renderer.rebuild()
    await asyncio.sleep(0.1)

    held = player.get_state()
    assert held.state is AudioGraphNodeState.PREPARING
    assert held.position_at(time.monotonic_ns()) == resumed[0]


async def test_playing_again_keeps_the_session_past_the_idle_timeout(
    player, renderer, published, monkeypatch
):
    monkeypatch.setattr(renderer_player, "IDLE_RELEASE_TIMEOUT_S", 0.1)
    player.on_interrupted(lambda position: player.append(2, _source(), position))
    await _playing_at(player, renderer, 30000)
    session = renderer.session_id
    mark = len(published.states)

    renderer.rebuild()
    await asyncio.sleep(0.3)

    states = published.since(mark)
    assert AudioGraphNodeState.STOPPED not in states
    assert states[-1] is AudioGraphNodeState.STREAMING
    assert renderer.session_id == session
    assert renderer.start_offsets["2"] >= 30000


async def test_a_resume_that_never_comes_is_reported_after_the_idle_timeout(
    player, renderer, published, resumed, monkeypatch
):
    monkeypatch.setattr(renderer_player, "IDLE_RELEASE_TIMEOUT_S", 0.1)
    lost = []
    player.on_session_lost(lost.append)
    await _playing_at(player, renderer, 30000)
    mark = len(published.states)

    renderer.rebuild()
    await asyncio.sleep(0.3)

    assert published.since(mark) == [AudioGraphNodeState.STOPPED]
    assert renderer.session_id is None
    assert lost == [None]


async def test_a_paused_track_is_reported_stopped(
    player, renderer, published, resumed
):
    """Resuming is for playback that was running, not for a deliberate pause:
    a settings change must not start making noise."""
    await _playing_at(player, renderer, 30000)
    player.pause()
    await asyncio.sleep(SETTLE_S)
    mark = len(published.states)

    renderer.rebuild()
    await asyncio.sleep(SETTLE_S)

    assert resumed == []
    assert published.since(mark) == [AudioGraphNodeState.STOPPED]


async def test_a_track_just_switched_to_is_resumed_from_its_start(
    player, renderer, published, resumed
):
    """The renderer has it on air from the gapless switch, before any state of
    its own has arrived."""
    await _playing_at(player, renderer, 30000)
    player.append(2, _source())
    await asyncio.sleep(SETTLE_S)
    renderer.switch_unreported()
    await asyncio.sleep(SETTLE_S)
    assert player.get_state().state is AudioGraphNodeState.SOURCE_CHANGED
    mark = len(published.states)

    renderer.rebuild()
    await asyncio.sleep(SETTLE_S)

    assert resumed == [0]
    assert published.since(mark) == []


async def test_a_renderer_that_does_not_say_still_reports_a_stop(
    player, renderer, published, resumed
):
    """One from before the interrupted field: its stop is taken as it comes."""
    renderer.reports_interruptions = False
    await _playing_at(player, renderer, 30000)
    mark = len(published.states)

    renderer.rebuild()
    await asyncio.sleep(SETTLE_S)

    assert resumed == []
    assert published.since(mark) == [AudioGraphNodeState.STOPPED]
