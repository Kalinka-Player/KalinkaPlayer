"""Moving playback from one renderer to another."""

import asyncio
from unittest.mock import Mock

import pytest

from kalinka_plugin_sdk import EventEmitter, PlaybackStateChangedEvent
from kalinka_plugin_sdk.datamodel import PlayerStateEnum
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_registry import (
    RendererRegistry,
    RendererUnavailable,
)
from kalinka_server.renderer_sessions import RendererBusy, SessionPool

from tests.fake_track_sources import FakeTrackSources, example_track
from tests.sim_renderer import SimRenderer


@pytest.fixture
def renderers():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    first = SimRenderer(registry, pool, renderer_id="rid-a")
    second = SimRenderer(registry, pool, renderer_id="rid-b")
    first.connect()
    second.connect()
    return registry, pool, first, second


@pytest.fixture
def emitter():
    return Mock(spec=EventEmitter)


@pytest.fixture
async def queue(renderers, emitter):
    registry, pool, _, _ = renderers
    playqueue = PlayQueueImpl(
        KalinkaConfig(), emitter, registry, pool, sources=FakeTrackSources()
    )
    await playqueue.__aenter__()
    yield playqueue
    await playqueue.__aexit__(None, None, None)


def _states(emitter) -> list[PlayerStateEnum]:
    return [
        call.args[0].state.state
        for call in emitter.dispatch.call_args_list
        if isinstance(call.args[0], PlaybackStateChangedEvent)
    ]


async def _play_on(queue, renderer) -> None:
    await queue.add([example_track()])
    await queue.play()
    await asyncio.sleep(0.2)
    assert renderer.current is not None, "expected playback to have started"


async def test_playback_moves_to_the_selected_renderer(queue, renderers, emitter):
    registry, _pool, first, second = renderers
    await _play_on(queue, first)

    await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.2)

    assert registry.active_id() == "rid-b"
    assert registry.selected_id == "rid-b"
    assert second.current is not None  # playing there now
    assert first.current is None  # and stopped here
    assert first.session_id is None  # the claim was given up
    assert queue.current_track_id == 0  # the same track


async def test_switching_to_a_renderer_we_cannot_drive_is_refused(
    queue, renderers
):
    """Resolution skips it, so accepting the pin would leave playback where it
    was while the client believed it had moved. The refusal says what to do."""
    registry, _pool, first, second = renderers
    await _play_on(queue, first)
    second.connect(compatible=False)

    with pytest.raises(RendererUnavailable, match="upgraded"):
        await queue.switch_renderer("rid-b")

    assert registry.active_id() == "rid-a"
    assert first.current is not None, "playback stayed put"


async def test_the_track_picks_up_where_it_had_reached(queue, renderers):
    """Carried as the source's start offset, not a seek behind it: a seek this
    early has no stream to address yet, and would land at zero."""
    _registry, _pool, first, second = renderers
    await _play_on(queue, first)
    first.report_position(5000)

    await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.2)

    started = [
        command
        for command in second.commands
        if command.WhichOneof("op") == "enqueue_source"
    ]
    assert len(started) == 1
    offset = started[0].enqueue_source.source.start_offset_ms
    assert 5000 <= offset < 5500, "where it had reached, plus the switch itself"
    assert not [
        command for command in second.commands if command.WhichOneof("op") == "seek"
    ]


async def test_a_paused_track_moves_without_drifting(queue, renderers):
    """Only a running stream advances, so a switch made while paused starts
    exactly where it was left, however long the pause lasted."""
    _registry, _pool, first, second = renderers
    await _play_on(queue, first)
    first.report_position(5000)
    await queue.pause(True)
    await asyncio.sleep(0.2)

    await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.2)

    started = next(
        command
        for command in second.commands
        if command.WhichOneof("op") == "enqueue_source"
    )
    assert started.enqueue_source.source.start_offset_ms == 5000


async def test_the_switch_is_visible_as_a_stop(queue, renderers, emitter):
    """The old renderer really does stop, and clients are told so rather than
    being shown continuous playback the speakers are not producing."""
    _registry, _pool, first, _second = renderers
    await _play_on(queue, first)
    emitter.reset_mock()

    await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.2)

    states = _states(emitter)
    assert PlayerStateEnum.STOPPED in states
    assert states[-1] == PlayerStateEnum.PLAYING
    assert states.index(PlayerStateEnum.STOPPED) < states.index(PlayerStateEnum.PLAYING)


async def test_a_renderer_that_will_not_have_us_leaves_playback_alone(
    queue, renderers
):
    """The point of claiming the target first: a refusal costs nothing."""
    registry, _pool, first, second = renderers
    await _play_on(queue, first)
    playing = first.current
    second.accept = False

    with pytest.raises(RendererBusy):
        await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.05)

    assert first.current == playing  # never interrupted
    assert first.session_id is not None
    assert second.session_id is None
    assert registry.active_id() == "rid-a"
    assert registry.selected_id is None


async def test_switching_with_nothing_playing_selects_but_claims_nothing(
    queue, renderers
):
    """Selecting a renderer is not a reason to hold one; the choice takes
    effect at the next play."""
    registry, _pool, first, second = renderers

    await queue.switch_renderer("rid-b")

    assert registry.active_id() == "rid-b"
    assert registry.selected_id == "rid-b"
    assert first.session_id is None
    assert second.session_id is None


async def test_switching_to_an_offline_renderer_is_refused(queue, renderers):
    """It cannot be claimed, so playback stays where it is and so does the pin."""
    registry, _pool, first, second = renderers
    await _play_on(queue, first)
    registry.disconnect("rid-b", second, clean=False)

    with pytest.raises(RendererUnavailable):
        await queue.switch_renderer("rid-b")

    assert first.current is not None
    assert first.session_id is not None
    assert registry.active_id() == "rid-a"
    assert registry.selected_id is None


async def test_playback_on_an_offline_selected_renderer_fails_rather_than_moves(
    queue, renderers, emitter
):
    registry, _pool, first, second = renderers
    await queue.switch_renderer("rid-b")
    registry.disconnect("rid-b", second, clean=True)

    await queue.add([example_track()])
    await queue.play()
    await asyncio.sleep(0.2)

    assert PlayerStateEnum.ERROR in _states(emitter)
    assert registry.active_id() == "rid-b"
    assert first.session_id is None
    assert first.current is None
