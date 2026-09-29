"""A plugin playing on the renderer outside the play queue, end to end against
a simulated renderer: the takeover, the queue taking it back, and what
clients and the plugin are told along the way."""

import asyncio
import json
from unittest.mock import Mock

import pytest

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk import EventEmitter
from kalinka_plugin_sdk.datamodel import (
    Album,
    DeviceVolume,
    EntityId,
    EntityType,
    PlaybackControl,
    PlaybackMode,
    PlaybackState,
    PlayerStateEnum,
)
from kalinka_plugin_sdk.direct_playback import (
    HoldEnded,
    OutputUnavailable,
    RevokeReason,
    TransportKind,
)
from kalinka_plugin_sdk.events import (
    PlaybackControlChangedEvent,
    PlaybackStateChangedEvent,
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
    VolumeChangedEvent,
)
from kalinka_plugin_sdk.inputmodule import DirectUrl, Track, TrackInfo, TrackSource
from kalinka_server import renderer_player, state_keeper
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.direct_playback import DirectPlaybackService
from kalinka_server.playback_arbiter import PlaybackArbiter
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_output_device import RendererVolumeDevice
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import RendererBusy, SessionPool
from kalinka_server.renderer_test_tone import TonePlayer

from tests.sim_renderer import SimRenderer

SETTLE_S = 0.2
CONNECT_URL = "https://streaming.qobuz.test/111"


def _queue_track(track_id: str, gate: asyncio.Event | None = None) -> TrackInfo:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="test_source")

    async def source_retriever() -> TrackSource:
        if gate is not None:
            await gate.wait()
        return TrackSource(
            source=DirectUrl(url=f"http://example/{track_id}.flac"), format="FLAC"
        )

    return TrackInfo(
        id=entity,
        metadata=Track(
            id=entity,
            title=f"track{track_id}",
            duration=10,
            album=Album(id=entity, title="album"),
        ),
        source_retriever=source_retriever,
    )


def _connect_track(track_id: str = "111") -> tuple[TrackSource, Track]:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="qobuz")
    track = Track(
        id=entity,
        title="Connect song",
        duration=240,
        album=Album(id=entity, title="Connect album"),
    )
    source = TrackSource(
        source=DirectUrl(url=f"https://streaming.qobuz.test/{track_id}"),
        format="audio/flac",
    )
    return source, track


class Listener:
    def __init__(self):
        self.states: list[PlaybackState] = []
        self.finished = 0
        self.commands = []
        self.revoked: list[RevokeReason] = []
        self.volumes: list[DeviceVolume] = []

    def on_state(self, state):
        self.states.append(state)

    def on_finished(self):
        self.finished += 1

    def on_command(self, request):
        self.commands.append(request)

    def on_revoked(self, reason):
        self.revoked.append(reason)

    def on_volume(self, volume):
        self.volumes.append(volume)


class FakeRouter:
    def __init__(self, device=None):
        self.device = device

    def current(self):
        return self.device


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
def arbiter(emitter):
    return PlaybackArbiter(emitter)


@pytest.fixture
async def queue(renderer, emitter, arbiter):
    playqueue = PlayQueueImpl(
        KalinkaConfig(), emitter, renderer.registry, renderer.pool, arbiter=arbiter
    )
    await playqueue.__aenter__()
    yield playqueue
    await playqueue.__aexit__(None, None, None)


@pytest.fixture
def router():
    return FakeRouter()


@pytest.fixture
def device_bus():
    bus = EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent](  # type: ignore[type-var]
        initial_state=ExtDeviceState(power_on=False, volume=DeviceVolume())
    )
    yield bus
    bus.close()


@pytest.fixture
def direct(renderer, arbiter, router, device_bus, queue):
    return DirectPlaybackService(
        "qobuz",
        config=KalinkaConfig(),
        registry=renderer.registry,
        pool=renderer.pool,
        arbiter=arbiter,
        device_router=lambda: router,
        device_events=device_bus,
    )


def _events(emitter) -> list:
    return [call.args[0] for call in emitter.dispatch.call_args_list]


def _states(emitter) -> list[PlaybackState]:
    return [e.state for e in _events(emitter) if isinstance(e, PlaybackStateChangedEvent)]


def _holders(emitter) -> list:
    return [
        e.control.plugin_id
        for e in _events(emitter)
        if isinstance(e, PlaybackControlChangedEvent)
    ]


def _enqueued(renderer) -> list[str]:
    return [
        c.enqueue_source.source.uri
        for c in renderer.commands
        if c.WhichOneof("op") == "enqueue_source"
    ]


async def _play_queue(queue, renderer, *tracks: str) -> None:
    await queue.add([_queue_track(t) for t in tracks])
    await queue.play()
    await asyncio.sleep(SETTLE_S)
    assert renderer.current is not None, "expected the queue to be playing"


async def _hold_and_play(direct, listener):
    hold = await direct.acquire("Qobuz Connect", listener)
    await hold.play(*_connect_track())
    await asyncio.sleep(SETTLE_S)
    return hold


async def test_a_plugin_takes_over_a_playing_queue(queue, renderer, direct, emitter):
    await _play_queue(queue, renderer, "1", "2")
    queue_session = renderer.session_id
    emitter.reset_mock()
    listener = Listener()

    hold = await _hold_and_play(direct, listener)

    events = _events(emitter)
    stopped = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, PlaybackStateChangedEvent)
        and e.state.state is PlayerStateEnum.STOPPED
    )
    took = next(
        i for i, e in enumerate(events) if isinstance(e, PlaybackControlChangedEvent)
    )
    assert stopped < took, "the queue's stop is shown before the takeover"
    assert _holders(emitter) == ["qobuz"]
    assert renderer.session_id not in (None, queue_session)
    assert _enqueued(renderer)[-1] == CONNECT_URL
    shown = _states(emitter)[-1]
    assert shown.state is PlayerStateEnum.PLAYING
    assert shown.current_track.title == "Connect song"
    assert shown.index is None
    assert hold.renderer_id == SimRenderer.RENDERER_ID
    assert listener.states[-1].state is PlayerStateEnum.PLAYING


async def test_the_queue_keeps_its_place(queue, renderer, direct):
    await _play_queue(queue, renderer, "1", "2")
    await queue.next()
    await asyncio.sleep(SETTLE_S)

    await _hold_and_play(direct, Listener())

    assert len(queue.track_list) == 2
    assert queue.current_track_id == 1


async def test_the_mime_type_reaches_the_renderer(queue, renderer, direct):
    await _hold_and_play(direct, Listener())

    enqueue = [
        c for c in renderer.commands if c.WhichOneof("op") == "enqueue_source"
    ][-1]
    assert enqueue.enqueue_source.source.mime_type == "audio/flac"


async def test_playing_from_the_queue_takes_the_output_back(
    queue, renderer, direct, emitter, arbiter
):
    listener = Listener()
    await queue.add([_queue_track("1")])
    hold = await _hold_and_play(direct, listener)
    emitter.reset_mock()

    await queue.play()
    await asyncio.sleep(SETTLE_S)

    assert listener.revoked == [RevokeReason.QUEUE_PLAY]
    assert not hold.active
    assert not arbiter.held_by_plugin
    assert _holders(emitter) == [None]
    assert _enqueued(renderer)[-1] == "http://example/1.flac"
    assert renderer.current is not None
    assert _states(emitter)[-1].index == 0


async def test_a_revoked_hold_cannot_play(queue, renderer, direct):
    await queue.add([_queue_track("1")])
    hold = await _hold_and_play(direct, Listener())
    await queue.play()
    await asyncio.sleep(SETTLE_S)

    with pytest.raises(HoldEnded):
        await hold.play(*_connect_track("222"))
    assert _enqueued(renderer)[-1] == "http://example/1.flac"


async def test_transport_controls_reach_the_plugin_not_the_renderer(
    queue, renderer, direct
):
    listener = Listener()
    await queue.add([_queue_track("1"), _queue_track("2")])
    await _hold_and_play(direct, listener)
    before = len(renderer.commands)

    await queue.pause(True)
    await queue.pause(False)
    assert await queue.seek(1234) == 1234
    await queue.next()
    await queue.prev()
    await asyncio.sleep(SETTLE_S)

    assert [c.kind for c in listener.commands] == [
        TransportKind.PAUSE,
        TransportKind.RESUME,
        TransportKind.SEEK,
        TransportKind.NEXT,
        TransportKind.PREV,
    ]
    assert listener.commands[2].position_ms == 1234
    assert len(renderer.commands) == before
    assert queue.current_track_id == 0


async def test_stop_gives_the_output_back_without_playing(
    queue, renderer, direct, arbiter
):
    listener = Listener()
    await queue.add([_queue_track("1")])
    await _hold_and_play(direct, listener)

    await queue.stop()
    await asyncio.sleep(SETTLE_S)

    assert listener.revoked == [RevokeReason.TAKEN_BACK]
    assert not arbiter.held_by_plugin
    assert renderer.current is None
    assert "http://example/1.flac" not in _enqueued(renderer)


async def test_clients_asking_for_the_state_get_the_plugins(queue, direct):
    await queue.add([_queue_track("1")])
    await _hold_and_play(direct, Listener())

    state = await queue.get_playback_state()

    assert state.current_track.title == "Connect song"
    assert state.index is None


async def test_queue_edits_do_not_cover_the_plugins_playback(
    queue, direct, emitter
):
    await _hold_and_play(direct, Listener())
    emitter.reset_mock()

    await queue.add([_queue_track("1")])
    await queue.clear()
    await asyncio.sleep(SETTLE_S)

    assert _states(emitter) == []


async def test_the_end_of_a_track_is_the_plugins_to_act_on(
    queue, renderer, direct, emitter
):
    listener = Listener()
    hold = await _hold_and_play(direct, listener)
    session = renderer.session_id
    emitter.reset_mock()

    renderer.finish_current()
    await asyncio.sleep(SETTLE_S)
    await hold.play(*_connect_track("222"))
    await asyncio.sleep(SETTLE_S)

    assert listener.finished == 1
    assert all(s.state is not PlayerStateEnum.STOPPED for s in _states(emitter))
    assert renderer.session_id == session
    assert _enqueued(renderer)[-1] == "https://streaming.qobuz.test/222"
    assert hold.active


async def test_the_next_track_replaces_the_current_one(renderer, direct, queue):
    hold = await _hold_and_play(direct, Listener())

    await hold.play(*_connect_track("222"))
    await asyncio.sleep(SETTLE_S)

    assert renderer.current is not None
    assert renderer.queued == []
    assert _enqueued(renderer)[-1] == "https://streaming.qobuz.test/222"
    assert hold.state.current_track.id.id == "222"


async def test_releasing_returns_the_output_to_the_queue(
    queue, renderer, direct, emitter, arbiter
):
    listener = Listener()
    await queue.add([_queue_track("1")])
    hold = await _hold_and_play(direct, listener)
    emitter.reset_mock()

    await hold.release()
    await asyncio.sleep(SETTLE_S)

    assert listener.revoked == []
    assert not arbiter.held_by_plugin
    assert renderer.session_id is None
    assert _holders(emitter) == [None]
    shown = _states(emitter)[-1]
    assert shown.index == 0 and shown.current_track.title == "track1"


async def test_a_late_resolution_does_not_take_the_output_back(
    queue, renderer, direct
):
    """The queue was fetching a track's URL when the plugin took over; the
    answer arriving afterwards must not start the queue again."""
    gate = asyncio.Event()
    await queue.add([_queue_track("1", gate)])
    await queue.play()
    await asyncio.sleep(0.05)
    listener = Listener()
    hold = await _hold_and_play(direct, listener)

    gate.set()
    await asyncio.sleep(SETTLE_S)

    assert hold.active
    assert listener.revoked == []
    assert "http://example/1.flac" not in _enqueued(renderer)


async def test_an_idle_hold_gives_the_output_back(
    queue, renderer, direct, arbiter, monkeypatch
):
    monkeypatch.setattr(renderer_player, "IDLE_RELEASE_TIMEOUT_S", 0.05)
    listener = Listener()
    hold = await _hold_and_play(direct, listener)

    renderer.finish_current()
    await asyncio.sleep(SETTLE_S)

    assert listener.revoked == [RevokeReason.IDLE]
    assert not hold.active
    assert not arbiter.held_by_plugin


async def test_a_lost_renderer_ends_the_hold(queue, renderer, direct, arbiter):
    listener = Listener()
    hold = await _hold_and_play(direct, listener)

    renderer.pool.handle_renderer_removed(SimRenderer.RENDERER_ID)
    await asyncio.sleep(SETTLE_S)

    assert listener.revoked == [RevokeReason.OUTPUT_LOST]
    assert not hold.active
    assert not arbiter.held_by_plugin


async def test_a_renderer_that_restarts_mid_track_carries_on(
    queue, renderer, direct
):
    listener = Listener()
    hold = await _hold_and_play(direct, listener)
    renderer.report_position(30000)
    await asyncio.sleep(0.05)
    renderer.linked = False
    renderer.registry.disconnect(renderer.RENDERER_ID, renderer, clean=False)
    renderer.pool.suspend(renderer.RENDERER_ID, renderer)
    renderer.session_id = None
    renderer.current = None
    renderer.queued.clear()
    renderer.linked = True
    renderer.connect()

    await renderer.pool.reconcile(
        renderer_id=renderer.RENDERER_ID,
        reported_session_id="",
        reported_owner_server_id="test-server-id",
        ws_session=renderer,
    )
    await asyncio.sleep(0.3)

    assert hold.active
    assert listener.revoked == []
    assert renderer.current is not None
    offset = [
        c.enqueue_source.source.start_offset_ms
        for c in renderer.commands
        if c.WhichOneof("op") == "enqueue_source"
    ][-1]
    assert offset >= 30000


async def test_without_a_renderer_nothing_is_taken(emitter, arbiter, device_bus):
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    playqueue = PlayQueueImpl(KalinkaConfig(), emitter, registry, pool, arbiter=arbiter)
    await playqueue.__aenter__()
    try:
        direct = DirectPlaybackService(
            "qobuz",
            config=KalinkaConfig(),
            registry=registry,
            pool=pool,
            arbiter=arbiter,
            device_router=lambda: FakeRouter(),
            device_events=device_bus,
        )
        with pytest.raises(OutputUnavailable):
            await direct.acquire("Qobuz Connect", Listener())
        assert not arbiter.held_by_plugin
    finally:
        await playqueue.__aexit__(None, None, None)


async def test_a_renderer_another_core_holds_refuses(
    queue, renderer, direct, arbiter
):
    renderer.accept = False

    with pytest.raises(OutputUnavailable):
        await direct.acquire("Qobuz Connect", Listener())

    assert not arbiter.held_by_plugin


async def test_a_second_acquire_ends_the_first_hold(queue, direct, arbiter):
    first_listener = Listener()
    first = await _hold_and_play(direct, first_listener)

    second = await direct.acquire("Qobuz Connect", Listener())

    assert not first.active
    assert first_listener.revoked == []
    assert second.active
    assert arbiter.owns(second)


async def test_volume_goes_through_the_device_in_charge(
    queue, renderer, direct, router, device_bus
):
    device = await RendererVolumeDevice(renderer.registry, renderer.pool, device_bus).start()
    router.device = device
    try:
        hold = await _hold_and_play(direct, Listener())

        await hold.set_volume(30)
        await asyncio.sleep(0.05)

        assert renderer.volume == 30
        assert (await hold.get_volume()).current_volume == 30
    finally:
        await device.shutdown()


class StepDevice:
    """A volume device counting in its own steps, as an amplifier does."""

    def __init__(self, max_volume: int):
        self.volume = DeviceVolume(max_volume=max_volume, current_volume=0)
        self.set_to: list[int] = []

    async def get_volume(self):
        return self.volume

    async def set_volume(self, volume):
        self.set_to.append(volume)


async def test_a_volume_percentage_is_of_the_devices_range(queue, direct, router):
    router.device = StepDevice(max_volume=160)
    hold = await _hold_and_play(direct, Listener())

    await hold.set_volume(50)

    assert router.device.set_to == [80]


async def test_the_plugin_hears_the_volume_and_every_change_to_it(
    queue, renderer, direct, router, device_bus
):
    device = await RendererVolumeDevice(renderer.registry, renderer.pool, device_bus).start()
    router.device = device
    listener = Listener()
    try:
        hold = await _hold_and_play(direct, listener)
        await asyncio.sleep(SETTLE_S)
        assert listener.volumes[-1].current_volume == 40

        await device.set_volume(55)
        await asyncio.sleep(SETTLE_S)
        assert listener.volumes[-1].current_volume == 55

        renderer.turn_knob(20)
        await asyncio.sleep(SETTLE_S)
        assert listener.volumes[-1].current_volume == 20

        await hold.release()
        heard = len(listener.volumes)
        device_bus.dispatch(
            VolumeChangedEvent.model_construct(
                event_type=ExtDeviceEventType.VolumeChanged,
                volume=DeviceVolume(max_volume=100, current_volume=70),
            )
        )
        await asyncio.sleep(SETTLE_S)
        assert len(listener.volumes) == heard
    finally:
        await device.shutdown()


async def test_the_speaker_test_displaces_the_plugin_and_says_so(
    queue, renderer, direct, arbiter
):
    listener = Listener()
    hold = await _hold_and_play(direct, listener)
    tones = TonePlayer(renderer.registry, renderer.pool, arbiter.vacate)

    await tones.play(SimRenderer.RENDERER_ID, "left", "http://10.0.0.5:8000/left.flac")
    await asyncio.sleep(0.05)

    assert listener.revoked == [RevokeReason.OTHER_HOLDER]
    assert not hold.active
    await tones.shutdown()


async def test_a_hold_is_not_saved_with_the_queue(tmp_path):
    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](  # type: ignore[type-var]
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    bus.dispatch(
        PlaybackControlChangedEvent(
            control=PlaybackControl.exclusive("qobuz", "Qobuz Connect")
        )
    )
    assert bus.get_snapshot().playback_control.is_exclusive
    path = tmp_path / "state.json"
    state_keeper.set_state_file(str(path))

    await state_keeper.save_state(bus)
    bus.close()

    assert "playback_control" not in json.loads(path.read_text())


async def _replayed_control(bus) -> PlaybackControl:
    async with bus.stream(list(PlayQueueEventType)) as events:
        replay = await asyncio.wait_for(anext(events), 2)
    return replay.state.playback_control


async def test_a_client_joining_mid_hold_is_told_the_queue_is_not_playing(
    renderer, device_bus
):
    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](  # type: ignore[type-var]
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    arbiter = PlaybackArbiter(bus)
    queue = PlayQueueImpl(
        KalinkaConfig(), bus, renderer.registry, renderer.pool, arbiter=arbiter
    )
    await queue.__aenter__()
    direct = DirectPlaybackService(
        "qobuz",
        config=KalinkaConfig(),
        registry=renderer.registry,
        pool=renderer.pool,
        arbiter=arbiter,
        device_router=lambda: FakeRouter(),
        device_events=device_bus,
    )
    try:
        assert not (await _replayed_control(bus)).is_exclusive

        hold = await _hold_and_play(direct, Listener())
        assert await _replayed_control(bus) == PlaybackControl.exclusive(
            "qobuz", "Qobuz Connect"
        )

        await hold.release()
        assert not (await _replayed_control(bus)).is_exclusive
    finally:
        await queue.__aexit__(None, None, None)
        bus.close()


def _second_renderer(renderer: SimRenderer) -> SimRenderer:
    other = SimRenderer(renderer.registry, renderer.pool, renderer_id="rid-b")
    other.connect()
    return other


async def test_the_plugins_playback_moves_with_the_renderer(
    queue, renderer, direct, emitter
):
    other = _second_renderer(renderer)
    listener = Listener()
    hold = await _hold_and_play(direct, listener)
    renderer.report_position(30000)
    await asyncio.sleep(0.05)
    emitter.reset_mock()
    listener.states.clear()

    await queue.switch_renderer("rid-b")
    await asyncio.sleep(SETTLE_S)

    moved = [
        c.enqueue_source.source
        for c in other.commands
        if c.WhichOneof("op") == "enqueue_source"
    ]
    assert [m.uri for m in moved] == [CONNECT_URL]
    assert moved[0].start_offset_ms >= 30000
    assert renderer.session_id is None
    assert other.current is not None
    assert hold.active and hold.renderer_id == "rid-b"
    assert listener.revoked == []
    assert renderer.registry.active_id() == "rid-b"
    assert renderer.registry.selected_id == "rid-b"
    shown = [s.state for s in _states(emitter)] + [s.state for s in listener.states]
    assert PlayerStateEnum.STOPPED not in shown
    assert _states(emitter)[-1].current_track.title == "Connect song"


async def test_a_paused_track_moves_paused(queue, renderer, direct, emitter):
    other = _second_renderer(renderer)
    hold = await _hold_and_play(direct, Listener())
    await hold.pause()
    await asyncio.sleep(0.05)

    await queue.switch_renderer("rid-b")
    await asyncio.sleep(SETTLE_S)

    ops = [c.WhichOneof("op") for c in other.commands]
    assert ops.index("enqueue_source") < ops.index("pause")
    assert _states(emitter)[-1].state is PlayerStateEnum.PAUSED


async def test_a_busy_target_leaves_the_playback_where_it_was(
    queue, renderer, direct
):
    other = _second_renderer(renderer)
    other.accept = False
    hold = await _hold_and_play(direct, Listener())
    playing = renderer.current

    with pytest.raises(RendererBusy):
        await queue.switch_renderer("rid-b")

    assert hold.active and hold.renderer_id == SimRenderer.RENDERER_ID
    assert renderer.current == playing
    assert renderer.registry.selected_id is None


async def test_moving_to_where_it_plays_only_pins_the_choice(
    queue, renderer, direct
):
    hold = await _hold_and_play(direct, Listener())
    before = len(renderer.commands)

    await queue.switch_renderer(SimRenderer.RENDERER_ID)

    assert len(renderer.commands) == before
    assert renderer.registry.selected_id == SimRenderer.RENDERER_ID
    assert hold.renderer_id == SimRenderer.RENDERER_ID


async def test_the_queue_plays_where_the_plugin_was_moved(
    queue, renderer, direct
):
    other = _second_renderer(renderer)
    await queue.add([_queue_track("1")])
    await _hold_and_play(direct, Listener())
    await queue.switch_renderer("rid-b")
    await asyncio.sleep(SETTLE_S)

    await queue.play()
    await asyncio.sleep(SETTLE_S)

    assert _enqueued(other)[-1] == "http://example/1.flac"


async def test_sequential_source_reports_timeline_offset(queue, renderer, direct):
    listener = Listener()
    hold = await direct.acquire('Spotify Connect', listener)
    source, track = _connect_track()
    source = source.model_copy(update={'sequential': True, 'timeline_offset_ms': 45000})
    await hold.play(source, track)
    await asyncio.sleep(SETTLE_S)
    renderer.report_position(1000)
    await asyncio.sleep(.05)
    assert listener.states[-1].position >= 46000
    enqueue = [c for c in renderer.commands if c.WhichOneof('op') == 'enqueue_source'][-1]
    assert enqueue.enqueue_source.source.start_offset_ms == 0


async def test_sequential_source_is_revoked_on_replay(queue, renderer, direct):
    listener = Listener()
    hold = await direct.acquire('Spotify Connect', listener)
    source, track = _connect_track()
    source = source.model_copy(update={'sequential': True})
    await hold.play(source, track)
    await asyncio.sleep(SETTLE_S)
    before = len(_enqueued(renderer))
    await hold._replay(1000)
    await asyncio.sleep(.05)
    assert not hold.active
    assert listener.revoked == [RevokeReason.OUTPUT_LOST]
    assert len(_enqueued(renderer)) == before


async def test_sequential_source_target_change_revokes(queue, renderer, direct):
    listener = Listener()
    hold = await direct.acquire('Spotify Connect', listener)
    source, track = _connect_track()
    source = source.model_copy(update={'sequential': True})
    await hold.play(source, track)
    await asyncio.sleep(SETTLE_S)
    commit = Mock()
    await hold.move_to('different-renderer', commit)
    await asyncio.sleep(.05)
    assert not hold.active
    assert listener.revoked == [RevokeReason.OUTPUT_LOST]
    commit.assert_called_once()


async def test_sequential_feedback_polls_actual_renderer_position(queue, renderer, direct):
    listener = Listener()
    hold = await direct.acquire('Spotify Connect', listener)
    source, track = _connect_track()
    source = source.model_copy(update={'sequential': True})
    await hold.play(source, track)
    await asyncio.sleep(SETTLE_S)
    renderer.position_ms = 10000  # No state transition/event is emitted.
    await asyncio.sleep(1.1)
    assert listener.states[-1].position >= 10000
    assert any(c.WhichOneof('op') == 'request_snapshot' for c in renderer.commands)


def _snapshot_requests(renderer) -> int:
    return sum(c.WhichOneof('op') == 'request_snapshot' for c in renderer.commands)


async def test_sequential_source_refuses_an_offset_or_a_seek(queue, renderer, direct):
    hold = await direct.acquire('Spotify Connect', Listener())
    source, track = _connect_track()
    source = source.model_copy(update={'sequential': True})
    with pytest.raises(ValueError):
        await hold.play(source, track, start_offset_ms=1000)
    await hold.play(source, track)
    await asyncio.sleep(SETTLE_S)
    with pytest.raises(ValueError):
        await hold.seek(30000)
    await asyncio.sleep(SETTLE_S)
    assert not any(c.WhichOneof('op') == 'seek' for c in renderer.commands)


async def test_a_finished_sequential_source_is_finished_once(queue, renderer, direct):
    listener = Listener()
    hold = await direct.acquire('Spotify Connect', listener)
    source, track = _connect_track()
    await hold.play(source.model_copy(update={'sequential': True}), track)
    await asyncio.sleep(SETTLE_S)
    renderer.finish_current()
    await asyncio.sleep(SETTLE_S)
    polled = _snapshot_requests(renderer)
    await asyncio.sleep(2.2)
    assert listener.finished == 1
    assert _snapshot_requests(renderer) == polled


async def test_a_paused_sequential_source_is_not_polled(queue, renderer, direct):
    hold = await direct.acquire('Spotify Connect', Listener())
    source, track = _connect_track()
    await hold.play(source.model_copy(update={'sequential': True}), track)
    await asyncio.sleep(SETTLE_S)
    await hold.pause()
    await asyncio.sleep(SETTLE_S)
    polled = _snapshot_requests(renderer)
    await asyncio.sleep(1.2)
    assert _snapshot_requests(renderer) == polled


async def test_a_seekable_source_is_not_polled(queue, renderer, direct):
    hold = await direct.acquire('Spotify Connect', Listener())
    await hold.play(*_connect_track())
    await asyncio.sleep(1.2)
    assert _snapshot_requests(renderer) == 0
