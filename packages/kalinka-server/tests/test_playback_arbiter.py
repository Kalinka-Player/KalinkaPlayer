"""Who drives the output: handover between the play queue and plugin holders."""

import asyncio
from typing import Optional
from unittest.mock import Mock

import pytest

from kalinka_plugin_sdk import EventEmitter
from kalinka_plugin_sdk.datamodel import PlaybackControl, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import (
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_sdk.events import (
    PlaybackControlChangedEvent,
    PlaybackStateChangedEvent,
)
from kalinka_server.playback_arbiter import PlaybackArbiter


class FakeOwner:
    def __init__(
        self,
        control: PlaybackControl,
        state: PlayerStateEnum,
        renderer_id: Optional[str] = "rid-a",
    ):
        self.control = control
        self.renderer_id = renderer_id
        self.state = PlaybackState(state=state)
        self.preempted: list[RevokeReason] = []
        self.commands: list[TransportRequest] = []
        self.preempt_gate: Optional[asyncio.Event] = None
        self.preempt_error: Optional[Exception] = None

    async def preempt(self, reason: RevokeReason) -> None:
        self.preempted.append(reason)
        if self.preempt_gate is not None:
            await self.preempt_gate.wait()
        if self.preempt_error is not None:
            raise self.preempt_error

    def snapshot(self) -> PlaybackState:
        return self.state

    def handle_command(self, request: TransportRequest) -> bool:
        if not self.control.is_exclusive:
            return False
        self.commands.append(request)
        return True


class FakeQueue(FakeOwner):
    def __init__(self):
        super().__init__(PlaybackControl.queue(), PlayerStateEnum.PAUSED)
        self.released: list[str] = []

    async def release_renderer(self, renderer_id: str) -> bool:
        self.released.append(renderer_id)
        return True


def _plugin(plugin_id: str = "qobuz", renderer_id: str = "rid-a") -> FakeOwner:
    return FakeOwner(
        PlaybackControl.exclusive(plugin_id, "Qobuz Connect"),
        PlayerStateEnum.PLAYING,
        renderer_id,
    )


@pytest.fixture
def emitter():
    return Mock(spec=EventEmitter)


@pytest.fixture
def queue():
    return FakeQueue()


@pytest.fixture
def arbiter(emitter, queue):
    arbiter = PlaybackArbiter(emitter)
    arbiter.set_queue(queue)
    return arbiter


def _dispatched(emitter) -> list:
    events = []
    for call in emitter.dispatch.call_args_list:
        event = call.args[0]
        if isinstance(event, PlaybackControlChangedEvent):
            events.append(("control", event.control.plugin_id))
        elif isinstance(event, PlaybackStateChangedEvent):
            events.append(("state", event.state.state))
    return events


async def test_a_plugin_takes_the_output_from_the_queue(arbiter, emitter, queue):
    plugin = _plugin()

    await arbiter.acquire(plugin)

    assert queue.preempted == [RevokeReason.OTHER_HOLDER]
    assert arbiter.owns(plugin) and arbiter.held_by_plugin
    assert _dispatched(emitter) == [
        ("control", "qobuz"),
        ("state", PlayerStateEnum.PLAYING),
    ]


async def test_the_queue_takes_it_back_and_says_what_it_shows(arbiter, emitter, queue):
    plugin = _plugin()
    await arbiter.acquire(plugin)
    emitter.reset_mock()

    await arbiter.acquire(queue)

    assert plugin.preempted == [RevokeReason.QUEUE_PLAY]
    assert not arbiter.held_by_plugin
    assert arbiter.control == PlaybackControl.queue()
    assert _dispatched(emitter) == [
        ("control", None),
        ("state", PlayerStateEnum.PAUSED),
    ]


async def test_the_control_answers_who_drives_the_output(arbiter, queue):
    assert arbiter.control == PlaybackControl.queue()
    plugin = _plugin()

    await arbiter.acquire(plugin)
    assert arbiter.control == PlaybackControl.exclusive("qobuz", "Qobuz Connect")

    await arbiter.release(plugin)
    assert arbiter.control == PlaybackControl.queue()


async def test_acquiring_what_one_owns_changes_nothing(arbiter, emitter, queue):
    await arbiter.acquire(queue)

    assert queue.preempted == []
    emitter.dispatch.assert_not_called()


async def test_only_the_owner_reaches_clients(arbiter, emitter, queue):
    plugin = _plugin()
    await arbiter.acquire(plugin)
    emitter.reset_mock()

    arbiter.report(queue, PlaybackState(state=PlayerStateEnum.STOPPED))
    arbiter.report(plugin, PlaybackState(state=PlayerStateEnum.PAUSED))

    assert _dispatched(emitter) == [("state", PlayerStateEnum.PAUSED)]
    assert arbiter.current_state().state is PlayerStateEnum.PAUSED


async def test_controls_go_to_a_plugin_holder_only(arbiter, queue):
    seek = TransportRequest(TransportKind.SEEK, position_ms=1200)
    assert arbiter.forward(seek) is False

    plugin = _plugin()
    await arbiter.acquire(plugin)

    assert arbiter.forward(seek) is True
    assert plugin.commands == [seek]


async def test_a_plugin_letting_go_returns_the_output_to_the_queue(
    arbiter, emitter, queue
):
    plugin = _plugin()
    await arbiter.acquire(plugin)
    emitter.reset_mock()

    await arbiter.release(plugin)

    assert not arbiter.held_by_plugin
    assert plugin.preempted == []
    assert _dispatched(emitter) == [
        ("control", None),
        ("state", PlayerStateEnum.PAUSED),
    ]


async def test_a_late_release_from_a_displaced_holder_changes_nothing(
    arbiter, emitter
):
    first, second = _plugin("first"), _plugin("second")
    await arbiter.acquire(first)
    await arbiter.acquire(second)
    emitter.reset_mock()

    await arbiter.release(first)

    assert arbiter.owns(second)
    emitter.dispatch.assert_not_called()


async def test_taking_back_revokes_the_holder(arbiter, queue):
    plugin = _plugin()
    await arbiter.acquire(plugin)

    await arbiter.take_back()

    assert plugin.preempted == [RevokeReason.TAKEN_BACK]
    assert arbiter.owns(queue)


async def test_taking_back_from_the_queue_is_nothing(arbiter, queue):
    await arbiter.take_back()

    assert queue.preempted == []


async def test_vacating_a_renderer_stops_whoever_is_on_it(arbiter, queue):
    plugin = _plugin(renderer_id="rid-a")
    await arbiter.acquire(plugin)

    await arbiter.vacate("rid-a")

    assert plugin.preempted == [RevokeReason.OTHER_HOLDER]
    assert arbiter.owns(queue)
    assert queue.released == ["rid-a"]


async def test_vacating_another_renderer_leaves_the_holder(arbiter, queue):
    plugin = _plugin(renderer_id="rid-a")
    await arbiter.acquire(plugin)

    await arbiter.vacate("rid-b")

    assert plugin.preempted == []
    assert arbiter.owns(plugin)
    assert queue.released == ["rid-b"]


async def test_the_owner_changes_only_once_its_predecessor_let_go(arbiter, queue):
    """While the queue is being preempted it still owns the output, so its own
    claims take the fast path instead of waiting on the lock."""
    queue.preempt_gate = asyncio.Event()
    plugin = _plugin()
    taking = asyncio.create_task(arbiter.acquire(plugin))
    await asyncio.sleep(0)

    assert arbiter.owns(queue)
    await asyncio.wait_for(arbiter.acquire(queue), 0.1)

    queue.preempt_gate.set()
    await taking
    assert arbiter.owns(plugin)


async def test_a_failing_preempt_still_hands_the_output_over(arbiter, queue):
    plugin = _plugin()
    await arbiter.acquire(plugin)
    plugin.preempt_error = RuntimeError("teardown failed")

    await arbiter.acquire(queue)

    assert arbiter.owns(queue)


async def test_shutdown_tells_the_holder(arbiter, queue):
    plugin = _plugin()
    await arbiter.acquire(plugin)

    await arbiter.shutdown()

    assert plugin.preempted == [RevokeReason.SHUTDOWN]
    assert arbiter.owns(queue)
