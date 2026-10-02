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
from kalinka_plugin_sdk.events import PlaybackControlChangedEvent
from kalinka_server.external_playback import ExternalPlaybackService
from kalinka_server.playback_arbiter import PlaybackArbiter


@pytest.fixture
def emitter():
    return Mock()


@pytest.fixture
def rig(emitter):
    arbiter = PlaybackArbiter(emitter)
    queue = Mock(control=PlaybackControl.queue(), renderer_id=None)
    queue.preempt = AsyncMock()
    queue.snapshot.return_value = PlaybackState(state=PlayerStateEnum.STOPPED)
    arbiter.set_queue(queue)
    listener = Mock(on_command=AsyncMock(), on_revoked=AsyncMock())
    return arbiter, queue, listener, ExternalPlaybackService("roon", arbiter)


def _queue_returned(emitter):
    released = asyncio.Event()

    def dispatched(event):
        if (
            isinstance(event, PlaybackControlChangedEvent)
            and not event.control.is_exclusive
        ):
            released.set()

    emitter.dispatch.side_effect = dispatched
    return released


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


@pytest.mark.parametrize("operation", ["queue", "plugin", "stop", "shutdown", "vacate"])
async def test_cancelled_handover_finishes_stopping_before_returning_to_queue(
    rig, operation
):
    arbiter, queue, listener, _ = rig
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def stop(reason):
        entered.set()
        await stopped.wait()

    listener.on_revoked.side_effect = stop
    service = ExternalPlaybackService("roon", arbiter, lambda: "local")
    hold = await service.acquire("Roon", listener)
    operations = {
        "queue": lambda: arbiter.acquire(queue),
        "plugin": lambda: ExternalPlaybackService("other", arbiter).acquire(
            "Other", Mock(on_command=AsyncMock(), on_revoked=AsyncMock())
        ),
        "stop": arbiter.take_back,
        "shutdown": arbiter.shutdown,
        "vacate": lambda: arbiter.vacate("local"),
    }
    taking = asyncio.create_task(operations[operation]())
    await asyncio.wait_for(entered.wait(), 1)
    try:
        taking.cancel()
        await asyncio.sleep(0)
        # Repeated cancellation must not cut short audio teardown either.
        taking.cancel()
        await asyncio.sleep(0)
        assert arbiter.owns(hold)
        assert not taking.done()
    finally:
        stopped.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(taking, 1)
    assert arbiter.owns(queue)
    assert not arbiter.held_by_plugin
    listener.on_revoked.assert_awaited_once()
    with pytest.raises(HoldEnded):
        hold.report(PlaybackState())
    # Cancellation must not prevent the next source from acquiring playback.
    next_hold = await service.acquire("Roon again", listener)
    await next_hold.release()


async def test_cancelled_renderer_change_releases_after_stop_without_committing(
    rig, emitter
):
    arbiter, queue, listener, service = rig
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def stop(reason):
        entered.set()
        await stopped.wait()

    listener.on_revoked.side_effect = stop
    hold = await service.acquire("Roon", listener)
    released = _queue_returned(emitter)
    commit = Mock()
    moving = asyncio.create_task(arbiter.move_holder("other", commit))
    await asyncio.wait_for(entered.wait(), 1)
    moving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await moving
    assert arbiter.owns(hold)
    stopped.set()
    # Nobody explicitly releases again after the caller has gone away.
    await asyncio.wait_for(released.wait(), 1)
    assert arbiter.owns(queue)
    commit.assert_not_called()


async def test_cancelled_release_does_not_leave_inactive_owner(rig, emitter):
    arbiter, queue, listener, service = rig
    entered, finished = asyncio.Event(), asyncio.Event()

    async def command(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await finished.wait()

    listener.on_command.side_effect = command
    hold = await service.acquire("Roon", listener)
    released = _queue_returned(emitter)
    arbiter.forward(TransportRequest(TransportKind.PAUSE))
    await asyncio.wait_for(entered.wait(), 1)
    releasing = asyncio.create_task(hold.release())
    await asyncio.sleep(0)
    releasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    finished.set()
    # The caller was cancelled, so nobody can explicitly release again.
    # Wait for the ownership event, without participating in the cleanup.
    await asyncio.wait_for(released.wait(), 1)
    assert arbiter.owns(queue)
    assert not hold.active
    listener.on_revoked.assert_not_called()


async def test_cancelled_acquire_waiting_for_lock_does_not_preempt_current_owner(rig):
    arbiter, _, listener, service = rig
    hold = await service.acquire("Roon", listener)
    # A different ownership operation has the lock, before any preemption.
    async with arbiter._lock:
        taking = asyncio.create_task(
            ExternalPlaybackService("other", arbiter).acquire(
                "Other", Mock(on_command=AsyncMock(), on_revoked=AsyncMock())
            )
        )
        await asyncio.sleep(0)
        taking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(taking, 1)
    assert hold.active and arbiter.owns(hold)
    listener.on_revoked.assert_not_called()
    await hold.release()


async def test_release_cleanup_cannot_displace_a_later_owner(rig):
    arbiter, _, listener, service = rig
    hold = await service.acquire("Roon", listener)
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def stop(reason):
        entered.set()
        await stopped.wait()

    listener.on_revoked.side_effect = stop
    moving = asyncio.create_task(arbiter.move_holder("other-renderer", Mock()))
    await asyncio.wait_for(entered.wait(), 1)
    moving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await moving
    taking = asyncio.create_task(
        ExternalPlaybackService("other", arbiter).acquire(
            "Other", Mock(on_command=AsyncMock(), on_revoked=AsyncMock())
        )
    )
    stopped.set()
    next_hold = await asyncio.wait_for(taking, 1)
    await hold.release()
    assert arbiter.owns(next_hold) and next_hold.active
    assert arbiter.control.plugin_id == "other"
    await next_hold.release()


@pytest.mark.parametrize("operation", ["stop", "move"])
async def test_self_cancelled_stop_callback_cannot_strand_owner(rig, operation):
    arbiter, queue, listener, service = rig
    listener.on_revoked.side_effect = asyncio.CancelledError
    hold = await service.acquire("Roon", listener)
    request = (
        arbiter.take_back()
        if operation == "stop"
        else arbiter.move_holder("other", Mock())
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 1)
    assert arbiter.owns(queue)
    assert not hold.active
