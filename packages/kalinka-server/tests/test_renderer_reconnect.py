"""What clients are told while a renderer's link is down, and on its return."""

import asyncio
from unittest.mock import Mock

import pytest

from kalinka_plugin_sdk import EventEmitter, PlaybackStateChangedEvent
from kalinka_plugin_sdk.datamodel import PlayerStateEnum
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool

from tests.fake_track_sources import FakeTrackSources, example_track
from tests.sim_renderer import SimRenderer


@pytest.fixture
def renderer():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    sim = SimRenderer(registry, pool)
    sim.connect()
    return sim


@pytest.fixture
def emitter():
    return Mock(spec=EventEmitter)


@pytest.fixture
async def queue(renderer, emitter):
    playqueue = PlayQueueImpl(
        KalinkaConfig(),
        emitter,
        renderer.registry,
        renderer.pool,
        sources=FakeTrackSources(),
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


def _drop_link(renderer) -> None:
    """What the ws handler does when the socket goes: the session is kept for
    the renderer's return, not closed."""
    renderer.linked = False
    renderer.registry.disconnect(renderer.RENDERER_ID, renderer, clean=False)
    renderer.pool.suspend(renderer.RENDERER_ID, renderer)


def _restore_link(renderer, session_id: str) -> None:
    renderer.linked = True
    renderer.connect()
    return renderer.pool.reconcile(
        renderer_id=renderer.RENDERER_ID,
        reported_session_id=session_id,
        reported_owner_server_id="test-server-id",
        ws_session=renderer,
    )


async def _play(queue, renderer, *tracks: str) -> None:
    await queue.add([example_track(t) for t in tracks])
    await queue.play()
    await asyncio.sleep(0.2)
    assert renderer.current is not None


async def test_a_dropped_link_stops_claiming_playback_is_running(
    queue, renderer, emitter
):
    """Nothing was reported before: the position went on advancing on faith
    while the renderer might have been dead."""
    await _play(queue, renderer, "1")
    emitter.reset_mock()

    _drop_link(renderer)
    await asyncio.sleep(0.05)

    assert _states(emitter) == [PlayerStateEnum.BUFFERING]


async def test_a_paused_session_is_not_reported_as_buffering(
    queue, renderer, emitter
):
    """Paused is still true while the link is down, and saying "buffering"
    about a deliberate pause would be worse than saying nothing."""
    await _play(queue, renderer, "1")
    await queue.pause(True)
    await asyncio.sleep(0.1)
    emitter.reset_mock()

    _drop_link(renderer)
    await asyncio.sleep(0.05)

    assert _states(emitter) == []


async def test_the_renderer_coming_back_playing_settles_the_stall(
    queue, renderer, emitter
):
    await _play(queue, renderer, "1")
    session_id = renderer.session_id
    _drop_link(renderer)
    await asyncio.sleep(0.05)
    emitter.reset_mock()

    await _restore_link(renderer, session_id)
    await asyncio.sleep(0.1)

    assert _states(emitter) == [PlayerStateEnum.PLAYING]


async def test_a_track_that_ended_while_we_were_away_is_noticed_on_return(
    queue, renderer, emitter
):
    """The FINISHED itself was dropped at the renderer for want of anywhere to
    send it, so the snapshot on resume is the only evidence it happened."""
    await _play(queue, renderer, "1", "2")
    session_id = renderer.session_id
    _drop_link(renderer)
    renderer.finish_current()  # runs out with nobody listening
    await asyncio.sleep(0.05)
    emitter.reset_mock()

    await _restore_link(renderer, session_id)
    await asyncio.sleep(0.2)

    assert renderer.current is not None  # moved on to track 2
    assert queue.current_track_id == 1


def _restart(renderer) -> None:
    """The renderer reboots: it comes back a fresh instance, with no session
    and nothing playing."""
    renderer.session_id = None
    renderer.current = None
    renderer.queued.clear()
    renderer.position_ms = 0
    renderer.finished = False
    renderer.linked = True
    renderer.connect()


def _start_offsets(renderer) -> list[int]:
    return [
        command.enqueue_source.source.start_offset_ms
        for command in renderer.commands
        if command.WhichOneof("op") == "enqueue_source"
    ]


async def test_a_renderer_that_restarts_mid_track_carries_on_playing(
    queue, renderer
):
    """The session went with the instance that held it, but what was playing
    and how far in did not."""
    await _play(queue, renderer, "1", "2")
    _drop_link(renderer)
    await asyncio.sleep(0.05)
    _restart(renderer)

    await _restore_link(renderer, "")  # no session of its own to report
    await asyncio.sleep(0.3)

    assert renderer.current is not None, "expected the track to be back on"
    assert queue.current_track_id == 0, "the same track, not the next one"
    assert renderer.session_id is not None, "a fresh claim was made"


async def test_the_resumed_track_starts_where_it_had_reached(queue, renderer):
    await _play(queue, renderer, "1")
    await asyncio.sleep(0.25)  # let some of it play
    _drop_link(renderer)
    await asyncio.sleep(0.05)
    _restart(renderer)

    await _restore_link(renderer, "")
    await asyncio.sleep(0.3)

    offsets = _start_offsets(renderer)
    assert offsets[-1] > 0, "expected it to start where playback had reached"
    assert offsets[-1] < 5000, "the reboot must not be counted as playing time"


async def test_a_restart_reports_playing_rather_than_a_stop(
    queue, renderer, emitter
):
    """A stop nobody asked for is what this exists to avoid."""
    await _play(queue, renderer, "1")
    _drop_link(renderer)
    await asyncio.sleep(0.05)
    _restart(renderer)
    emitter.reset_mock()

    await _restore_link(renderer, "")
    await asyncio.sleep(0.3)

    states = _states(emitter)
    assert PlayerStateEnum.STOPPED not in states
    assert states[-1] == PlayerStateEnum.PLAYING


async def test_a_restart_while_paused_does_not_start_playing(
    queue, renderer, emitter
):
    """Resuming is for playback that was interrupted, not for a deliberate
    pause — a renderer coming back must not start making noise."""
    await _play(queue, renderer, "1")
    await queue.pause(True)
    await asyncio.sleep(0.1)
    _drop_link(renderer)
    await asyncio.sleep(0.05)
    _restart(renderer)

    await _restore_link(renderer, "")
    await asyncio.sleep(0.3)

    assert renderer.current is None
    assert renderer.session_id is None, "an idle queue claims nothing"


async def test_a_restart_with_nothing_playing_claims_nothing(queue, renderer):
    await queue.add([example_track("1")])
    _drop_link(renderer)
    await asyncio.sleep(0.05)
    _restart(renderer)

    await _restore_link(renderer, "")
    await asyncio.sleep(0.2)

    assert renderer.current is None
    assert renderer.session_id is None


async def test_a_stall_reports_where_playback_actually_got_to(
    queue, renderer, emitter
):
    """Reporting the last position the renderer named would rewind the display
    to whenever the track last changed state."""
    await _play(queue, renderer, "1")
    await asyncio.sleep(0.25)
    emitter.reset_mock()

    _drop_link(renderer)
    await asyncio.sleep(0.05)

    positions = [
        call.args[0].state.position
        for call in emitter.dispatch.call_args_list
        if isinstance(call.args[0], PlaybackStateChangedEvent)
    ]
    assert positions and positions[-1] > 0


async def test_the_snapshot_at_session_open_is_still_not_republished(
    queue, renderer, emitter
):
    """It only restates the idle baseline; acting on it would emit a STOPPED
    just as playback is about to start."""
    emitter.reset_mock()
    await _play(queue, renderer, "1")

    assert PlayerStateEnum.STOPPED not in _states(emitter)[1:]
