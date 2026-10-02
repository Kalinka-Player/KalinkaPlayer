"""External engines participate in the same source arbitration as renderers."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from kalinka_plugin_sdk.datamodel import PlaybackControl, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import (
    HoldEnded,
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_server.external_playback import ExternalPlaybackService
from kalinka_server.playback_arbiter import PlaybackArbiter


@pytest.fixture
def rig():
    emitter = Mock()
    arbiter = PlaybackArbiter(emitter)
    queue = Mock(control=PlaybackControl.queue(), renderer_id=None)
    queue.preempt = AsyncMock()
    queue.snapshot.return_value = PlaybackState(state=PlayerStateEnum.STOPPED)
    arbiter.set_queue(queue)
    listener = Mock(on_command=AsyncMock(), on_revoked=AsyncMock())
    return arbiter, queue, listener, ExternalPlaybackService("roon", arbiter)


async def test_metadata_without_renderer_and_queue_reports_are_hidden(rig):
    arbiter, queue, listener, service = rig
    hold = await service.acquire("Roon endpoint", listener)
    queue.preempt.assert_awaited_once_with(RevokeReason.OTHER_HOLDER)
    assert hold.renderer_id is None
    state = PlaybackState(state=PlayerStateEnum.PLAYING, position=12000, index=7)
    hold.report(state)
    arbiter.report(queue, PlaybackState(state=PlayerStateEnum.STOPPED))
    assert arbiter.current_state().position == 12000
    assert arbiter.current_state().timestamp_ns > 0
    assert arbiter.current_state().index is None
    assert state.timestamp_ns == 0
    await hold.release()
    assert not arbiter.held_by_plugin
    listener.on_revoked.assert_not_called()


async def test_queue_waits_for_external_stop_and_stale_reports_fail(rig):
    arbiter, queue, listener, service = rig
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def stop(reason):
        entered.set()
        await stopped.wait()

    listener.on_revoked.side_effect = stop
    hold = await service.acquire("Roon", listener)
    take = asyncio.create_task(arbiter.acquire(queue))
    await entered.wait()
    assert not hold.active
    assert arbiter.held_by_plugin
    assert not take.done()
    with pytest.raises(HoldEnded):
        hold.report(PlaybackState())
    stopped.set()
    await take
    assert not arbiter.held_by_plugin
    await hold.release()
    listener.on_revoked.assert_awaited_once_with(RevokeReason.QUEUE_PLAY)


@pytest.mark.parametrize(
    "method,reason",
    [("take_back", RevokeReason.TAKEN_BACK), ("shutdown", RevokeReason.SHUTDOWN)],
)
async def test_stop_and_shutdown_revoke_once(rig, method, reason):
    arbiter, _, listener, service = rig
    hold = await service.acquire("Roon", listener)
    await getattr(arbiter, method)()
    await getattr(arbiter, method)()
    await hold.release()
    listener.on_revoked.assert_awaited_once_with(reason)


async def test_other_plugin_stops_external_engine_before_taking_over(rig):
    arbiter, _, listener, service = rig
    hold = await service.acquire("Roon", listener)
    other = await ExternalPlaybackService("other", arbiter).acquire("Other", listener)
    assert not hold.active
    assert arbiter.control.plugin_id == "other"
    listener.on_revoked.assert_awaited_once_with(RevokeReason.OTHER_HOLDER)
    await other.release()


async def test_transport_and_output_selection(rig):
    arbiter, _, listener, service = rig
    hold = await service.acquire("Roon", listener)
    request = TransportRequest(TransportKind.PAUSE)
    assert arbiter.forward(request)
    for _ in range(5):
        await asyncio.sleep(0)
    listener.on_command.assert_awaited_once_with(request)
    commit = Mock()
    assert await arbiter.move_holder("another-renderer", commit)
    commit.assert_called_once()
    listener.on_revoked.assert_awaited_once_with(RevokeReason.OUTPUT_LOST)
    assert not hold.active
    assert not arbiter.forward(request)


async def test_failed_stop_callback_does_not_leak_holder(rig):
    arbiter, _, listener, service = rig
    listener.on_revoked.side_effect = RuntimeError("broken plugin")
    hold = await service.acquire("Roon", listener)
    await arbiter.take_back()
    assert not hold.active
    assert not arbiter.held_by_plugin


async def test_speaker_test_vacates_bound_renderer(rig):
    arbiter, queue, listener, _ = rig
    queue.release_renderer = AsyncMock()
    service = ExternalPlaybackService("roon", arbiter, lambda: "local")
    hold = await service.acquire("Roon", listener)
    await arbiter.vacate("local")
    assert not hold.active
    listener.on_revoked.assert_awaited_once_with(RevokeReason.OTHER_HOLDER)


async def test_plugin_can_release_from_transport_callback(rig):
    arbiter, _, listener, service = rig
    hold = await service.acquire("Roon", listener)
    done = asyncio.Event()

    async def command(request):
        await hold.release()
        done.set()

    listener.on_command.side_effect = command
    arbiter.forward(TransportRequest(TransportKind.PAUSE))
    await asyncio.wait_for(done.wait(), 1)
    assert not arbiter.held_by_plugin
    assert not hold.active
