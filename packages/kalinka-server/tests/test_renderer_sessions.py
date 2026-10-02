import asyncio

import pytest

from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry, RendererUnavailable
from kalinka_server.renderer_state import StateChange
from kalinka_server.renderer_sessions import (
    CloseReason,
    RendererBusy,
    SessionNotActive,
    SessionOpenFailed,
    SessionVolumePolicy,
    SessionPool,
    SessionState,
)

SERVER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_SERVER_ID = "22222222-2222-4222-8222-222222222222"
RENDERER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeWs:
    """Renderer end of the wire: records what the pool sends, replies on cue."""

    def __init__(self, pool=None, accept=True, busy_owner=""):
        self.pool = pool
        self.accept = accept
        self.busy_owner = busy_owner
        self.opened: list[str] = []
        self.closed: list[tuple[str, CloseReason]] = []
        self.commands: list[pb.Command] = []
        self.answer = True
        self.stall = False
        self.fail_send = False
        self.volume_policies = []

    async def send_session_open(
        self,
        session_id,
        force_fixed_output=False,
    ):
        self.opened.append(session_id)
        self.volume_policies.append(force_fixed_output)
        if self.answer:
            self.pool.handle_open_result(
                RENDERER_ID,
                session_id=session_id,
                accepted=self.accept,
                busy=not self.accept,
                detail="" if self.accept else "busy",
                owner_server_id=self.busy_owner,
            )

    async def send_command(self, session_id, command):
        if self.fail_send:
            raise RuntimeError("socket is closed")
        if self.stall:
            await asyncio.Event().wait()
        copied = pb.Command()
        copied.CopyFrom(command)
        self.commands.append(copied)

    def reject_last(
        self, session_id, command=pb.CONTROL_KIND_RESUME, detail="no session"
    ):
        rejection = pb.CommandRejected()
        rejection.session_id = session_id
        rejection.command = command
        rejection.at_unix_ms = 1700000000000
        rejection.detail = detail
        self.pool.handle_rejection(RENDERER_ID, rejection=rejection)

    def send_state(self, session_id, change, message):
        """The renderer reporting state, unprompted."""
        self.pool.handle_state(
            RENDERER_ID, session_id=session_id, change=change, message=message
        )

    async def send_session_close(self, session_id, reason):
        self.closed.append((session_id, reason))

    async def replace(self):
        pass


def make_pool(timeout_s=5.0):
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, SERVER_ID, timeout_s=timeout_s)
    registry.set_on_removed(pool.handle_renderer_removed)
    return registry, pool


def snapshot_message(state=pb.PLAYBACK_STATE_PLAYING, token="track-1"):
    snapshot = pb.StateSnapshot()
    snapshot.playback_state = state
    snapshot.current_source.uri = "http://core/stream/1"
    snapshot.current_source.mime_type = "audio/flac"
    snapshot.current_source.source_token = token
    snapshot.position_ms = 4200
    snapshot.position_valid = True
    snapshot.captured_at_unix_ms = 1700000000000
    snapshot.volume.supported = True
    snapshot.volume.current = 40
    snapshot.volume.max = 100
    snapshot.volume.backend = pb.VOLUME_BACKEND_HARDWARE
    return snapshot


def register(registry, ws, instance_id="inst-1"):
    registry.register(
        renderer_id=RENDERER_ID,
        instance_id=instance_id,
        friendly_name="Test Renderer",
        software_version="0.1.0",
        kind="native",
        platform={},
        session=ws,
    )


@pytest.mark.asyncio
async def test_the_volume_policy_rides_session_open():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)

    assert ws.volume_policies == []
    pool.set_volume_policy(
        lambda renderer_id: SessionVolumePolicy(force_fixed_output=True)
    )
    session = await pool.open(RENDERER_ID)

    assert ws.volume_policies == [True]
    await session.close()


async def test_no_policy_provider_sends_a_direct_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)

    session = await pool.open(RENDERER_ID)
    assert ws.volume_policies == [False]
    await session.close()


async def test_open_and_close():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)

    session = await pool.open(RENDERER_ID)
    assert session.state is SessionState.ACTIVE
    assert ws.opened == [session.session_id]
    assert pool.get(RENDERER_ID) is session

    closed: list = []
    session.on_closed(lambda s, reason: closed.append(reason))
    await session.close()

    assert closed == [CloseReason.CLOSED_BY_SERVER]
    assert session.state is SessionState.CLOSED
    assert pool.get(RENDERER_ID) is None
    assert ws.closed == [(session.session_id, CloseReason.CLOSED_BY_SERVER)]

    await session.close()  # idempotent
    assert closed == [CloseReason.CLOSED_BY_SERVER]


@pytest.mark.asyncio
async def test_open_requires_a_connected_renderer():
    registry, pool = make_pool()
    with pytest.raises(RendererUnavailable):
        await pool.open(RENDERER_ID)


@pytest.mark.asyncio
async def test_second_open_is_busy():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)

    await pool.open(RENDERER_ID)
    with pytest.raises(RendererBusy) as excinfo:
        await pool.open(RENDERER_ID)
    assert excinfo.value.owner_server_id == SERVER_ID


@pytest.mark.asyncio
async def test_renderer_refuses_as_busy():
    registry, pool = make_pool()
    ws = FakeWs(pool, accept=False, busy_owner=OTHER_SERVER_ID)
    register(registry, ws)

    with pytest.raises(RendererBusy) as excinfo:
        await pool.open(RENDERER_ID)
    assert excinfo.value.owner_server_id == OTHER_SERVER_ID
    assert pool.get(RENDERER_ID) is None


@pytest.mark.asyncio
async def test_open_times_out_and_releases_the_claim():
    registry, pool = make_pool(timeout_s=0.05)
    ws = FakeWs(pool)
    ws.answer = False
    register(registry, ws)

    with pytest.raises(asyncio.TimeoutError):
        await pool.open(RENDERER_ID)
    assert pool.get(RENDERER_ID) is None
    await asyncio.sleep(0)  # the compensating close is fire-and-forget
    assert ws.closed and ws.closed[0][1] == CloseReason.CLOSED_BY_SERVER


@pytest.mark.asyncio
async def test_reconnect_resumes_the_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    registry.disconnect(RENDERER_ID, ws, clean=False)
    pool.suspend(RENDERER_ID, ws)
    assert session.state is SessionState.SUSPENDED
    assert not session.connected

    ws2 = FakeWs(pool)
    register(registry, ws2)
    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id=session.session_id,
        reported_owner_server_id=SERVER_ID,
        ws_session=ws2,
    )

    assert session.state is SessionState.ACTIVE
    assert session.connected
    assert pool.get(RENDERER_ID) is session
    assert ws2.closed == []


@pytest.mark.asyncio
async def test_renderer_restart_closes_the_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    closed: list = []
    session.on_closed(lambda s, reason: closed.append(reason))

    registry.disconnect(RENDERER_ID, ws, clean=False)
    pool.suspend(RENDERER_ID, ws)

    ws2 = FakeWs(pool)
    register(registry, ws2, instance_id="inst-2")
    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id="",
        reported_owner_server_id="",
        ws_session=ws2,
    )

    assert closed == [CloseReason.RENDERER_RESTARTED]
    assert pool.get(RENDERER_ID) is None
    assert ws2.closed == []  # nothing to tell the renderer about


@pytest.mark.asyncio
async def test_orphaned_session_is_closed_as_stale():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)

    # This Core has no memory of the session (it restarted), but the renderer
    # is still running one it opened.
    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id="orphan-session",
        reported_owner_server_id=SERVER_ID,
        ws_session=ws,
    )

    assert ws.closed == [("orphan-session", CloseReason.STALE)]


@pytest.mark.asyncio
async def test_another_cores_session_is_left_alone():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)

    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id="not-ours",
        reported_owner_server_id=OTHER_SERVER_ID,
        ws_session=ws,
    )

    assert ws.closed == []
    assert pool.get(RENDERER_ID) is None


@pytest.mark.asyncio
async def test_reaped_renderer_closes_the_session():
    registry, pool = make_pool()
    registry.offline_timeout_s = 0.05
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    closed: list = []
    session.on_closed(lambda s, reason: closed.append(reason))

    registry.disconnect(RENDERER_ID, ws, clean=False)
    pool.suspend(RENDERER_ID, ws)
    await asyncio.sleep(0.15)

    assert closed == [CloseReason.RENDERER_LOST]
    assert pool.get(RENDERER_ID) is None


@pytest.mark.asyncio
async def test_goodbye_closes_the_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    closed: list = []
    session.on_closed(lambda s, reason: closed.append(reason))

    registry.disconnect(RENDERER_ID, ws, clean=True)
    pool.suspend(RENDERER_ID, ws)

    assert closed == [CloseReason.RENDERER_SHUTDOWN]


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_suspend_the_new_connection():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    ws2 = FakeWs(pool)
    register(registry, ws2)
    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id=session.session_id,
        reported_owner_server_id=SERVER_ID,
        ws_session=ws2,
    )
    pool.suspend(RENDERER_ID, ws)  # late teardown of the replaced connection

    assert session.state is SessionState.ACTIVE
    assert session.connected


@pytest.mark.asyncio
async def test_renderer_reported_error_closes_the_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    closed: list = []
    session.on_closed(lambda s, reason: closed.append(reason))

    pool.handle_closed(
        RENDERER_ID,
        session_id=session.session_id,
        renderer_error=True,
        detail="alsa device gone",
    )

    assert closed == [CloseReason.RENDERER_ERROR]
    assert pool.get(RENDERER_ID) is None


@pytest.mark.asyncio
async def test_drop_while_opening_fails_the_open():
    registry, pool = make_pool(timeout_s=5.0)
    ws = FakeWs(pool)
    ws.answer = False
    register(registry, ws)

    task = asyncio.create_task(pool.open(RENDERER_ID))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    pool.suspend(RENDERER_ID, ws)

    with pytest.raises(SessionOpenFailed):
        await task
    assert pool.get(RENDERER_ID) is None


@pytest.mark.asyncio
async def test_cancelling_open_releases_the_renderer():
    registry, pool = make_pool(timeout_s=30.0)
    ws = FakeWs(pool)
    ws.answer = False
    register(registry, ws)

    task = asyncio.create_task(pool.open(RENDERER_ID))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert pool.get(RENDERER_ID) is None
    assert ws.closed and ws.closed[0][1] == CloseReason.CLOSED_BY_SERVER


@pytest.mark.asyncio
async def test_reconnect_while_opening_does_not_adopt_the_session():
    """The renderer accepted but the reply was lost; it reconnects reporting
    the session. Adopting it would let the pending open() close it again."""
    registry, pool = make_pool(timeout_s=0.2)
    ws1 = FakeWs(pool)
    ws1.answer = False
    register(registry, ws1)

    task = asyncio.create_task(pool.open(RENDERER_ID))
    await asyncio.sleep(0.02)
    session = pool.get(RENDERER_ID)
    session_id = session.session_id

    ws2 = FakeWs(pool)
    register(registry, ws2)
    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id=session_id,
        reported_owner_server_id=SERVER_ID,
        ws_session=ws2,
    )

    with pytest.raises(SessionOpenFailed):
        await task
    # The renderer is told to drop it, on the connection that is actually live.
    assert ws2.closed == [(session_id, CloseReason.STALE)]
    assert pool.get(RENDERER_ID) is None


@pytest.mark.asyncio
async def test_commands_are_sent_unacknowledged():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    await session.set_source(
        "http://core/stream/1", mime_type="audio/flac", source_token="track-1"
    )
    await session.enqueue_source("http://core/stream/2", source_token="track-2")
    await session.remove_source("track-2")
    await session.clear_queue()
    await session.pause()
    await session.resume()
    await session.stop()
    await session.set_volume(35)
    await session.seek(12000)
    await session.request_snapshot()

    ops = [command.WhichOneof("op") for command in ws.commands]
    assert ops == [
        "set_source",
        "enqueue_source",
        "remove_source",
        "clear_queue",
        "pause",
        "resume",
        "stop",
        "set_volume",
        "seek",
        "request_snapshot",
    ]
    assert ws.commands[0].set_source.source.uri == "http://core/stream/1"
    assert ws.commands[0].set_source.source.mime_type == "audio/flac"
    assert ws.commands[7].set_volume.percent == 35
    assert ws.commands[8].seek.position_ms == 12000
    # Nothing came back, and the session is none the worse for it.
    assert session.state is SessionState.ACTIVE
    assert session.rejection is None


@pytest.mark.asyncio
async def test_a_refused_command_closes_the_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    closed: list = []
    session.on_closed(lambda s, reason: closed.append(reason))

    await session.resume()
    ws.reject_last(session.session_id, detail="no session is open on this renderer")

    assert closed == [CloseReason.REJECTED_BY_RENDERER]
    assert session.state is SessionState.CLOSED
    assert pool.get(RENDERER_ID) is None
    assert session.rejection == {
        "session_id": session.session_id,
        "command": "resume",
        "at_unix_ms": 1700000000000,
        "detail": "no session is open on this renderer",
    }


@pytest.mark.asyncio
async def test_a_rejection_for_another_session_is_ignored():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    ws.reject_last("a-session-we-closed-long-ago")

    assert session.state is SessionState.ACTIVE
    assert session.rejection is None


@pytest.mark.asyncio
async def test_commands_need_a_live_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    registry.disconnect(RENDERER_ID, ws, clean=False)
    pool.suspend(RENDERER_ID, ws)
    with pytest.raises(SessionNotActive):
        await session.resume()

    await session.close()
    with pytest.raises(SessionNotActive):
        await session.resume()
    assert len(ws.commands) == 0


@pytest.mark.asyncio
async def test_a_write_that_never_completes_times_out():
    registry, pool = make_pool(timeout_s=0.05)
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    ws.stall = True

    with pytest.raises(asyncio.TimeoutError):
        await session.resume()
    assert session.state is SessionState.ACTIVE  # a stuck write is not fatal


@pytest.mark.asyncio
async def test_a_dead_socket_reads_as_an_inactive_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    ws.fail_send = True

    with pytest.raises(SessionNotActive):
        await session.resume()


@pytest.mark.asyncio
async def test_state_messages_update_the_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)
    seen: list[str] = []
    session.on_state(lambda s, change, state: seen.append(change))

    ws.send_state(session.session_id, StateChange.SNAPSHOT, snapshot_message())
    assert session.snapshot["playback_state"] == "playing"
    assert session.snapshot["source_token"] == "track-1"
    assert session.snapshot["current_source"]["mime_type"] == "audio/flac"
    assert session.snapshot["volume"] == {
        "supported": True,
        "current": 40,
        "max": 100,
        "backend": "hardware",
    }

    changed = pb.PlaybackStateChanged()
    changed.state = pb.PLAYBACK_STATE_PAUSED
    changed.position_ms = 9000
    changed.position_valid = True
    changed.source_token = "track-1"
    changed.at_unix_ms = 1700000009000
    ws.send_state(session.session_id, StateChange.PLAYBACK, changed)

    assert session.snapshot["playback_state"] == "paused"
    assert session.snapshot["position_ms"] == 9000
    assert session.snapshot["current_source"]["source_token"] == "track-1"
    assert seen == [StateChange.SNAPSHOT, StateChange.PLAYBACK]


@pytest.mark.asyncio
async def test_state_for_another_session_is_ignored():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    ws.send_state("some-other-session", StateChange.SNAPSHOT, snapshot_message())

    assert session.snapshot["playback_state"] == "unspecified"


@pytest.mark.asyncio
async def test_a_resumed_session_takes_commands_again():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    registry.disconnect(RENDERER_ID, ws, clean=False)
    pool.suspend(RENDERER_ID, ws)

    ws2 = FakeWs(pool)
    register(registry, ws2)
    await pool.reconcile(
        renderer_id=RENDERER_ID,
        reported_session_id=session.session_id,
        reported_owner_server_id=SERVER_ID,
        ws_session=ws2,
    )
    await asyncio.sleep(0.01)  # the post-resume snapshot request is detached
    await session.resume()

    assert [c.WhichOneof("op") for c in ws2.commands] == ["request_snapshot", "resume"]


@pytest.mark.asyncio
async def test_shutdown_closes_every_session():
    registry, pool = make_pool()
    ws = FakeWs(pool)
    register(registry, ws)
    session = await pool.open(RENDERER_ID)

    await pool.shutdown()

    assert session.state is SessionState.CLOSED
    assert ws.closed == [(session.session_id, CloseReason.SHUTDOWN)]
    assert pool.list() == []
