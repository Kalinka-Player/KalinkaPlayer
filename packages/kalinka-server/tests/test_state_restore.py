"""Restoring the saved queue at startup, whether or not its modules are ready.

The queue comes back from the snapshot alone; a track's module is asked for
its source only when the track plays, through the same resolver the server
wires in.
"""

import asyncio
import inspect
from typing import Optional

import pytest

from kalinka_eventbus.bus import EventBus
from kalinka_plugin_sdk.datamodel import (
    Album,
    EntityId,
    EntityType,
    PlaybackMode,
    PlaybackState,
    PlayerStateEnum,
    Track,
)
from kalinka_plugin_sdk.events import (
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_plugin_sdk.inputmodule import (
    DirectUrl,
    InputModule,
    TrackInfo,
    TrackSource,
)
from kalinka_server import state_keeper
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.track_sources import ModuleTrackSources

from tests.sim_renderer import SimRenderer


def _make_track(track_id: str, source: str) -> Track:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source=source)
    album = EntityId(id="album-1", type=EntityType.ALBUM, source=source)
    return Track(
        id=entity,
        title=f"track-{track_id}",
        duration=180,
        album=Album(id=album, title="album-1"),
    )


def _url(track_id: str) -> str:
    return f"https://example.invalid/{track_id}.mp3"


class _OlderModule(InputModule):
    """A module written before SDK 3.8, as an out-of-tree plugin may be:
    get_track_info only, so it plays through the SDK's default
    get_track_source.

    Raises like a plugin still signing in until ``ready``; then answers for
    the ids it ``knows`` (every id when None), taking ``delay_s`` to do so.
    """

    def __init__(
        self,
        name: str,
        *,
        ready: bool = True,
        knows: Optional[set[str]] = None,
        delay_s: float = 0.0,
    ):
        self.name = name
        self.ready = ready
        self.knows = knows
        self.delay_s = delay_s
        self.asked: list[list[str]] = []

    def module_name(self) -> str:
        return self.name

    async def get_track_info(self, track_ids):
        self.asked.append(list(track_ids))
        await asyncio.sleep(self.delay_s)
        if not self.ready:
            raise RuntimeError(f"{self.name.capitalize()} is starting up.")
        return [
            self._info(track_id)
            for track_id in track_ids
            if self.knows is None or track_id in self.knows
        ]

    def _info(self, track_id: str) -> TrackInfo:
        async def source() -> TrackSource:
            return TrackSource(
                source=DirectUrl(url=_url(track_id)), format="audio/mpeg"
            )

        return TrackInfo(
            id=EntityId(id=track_id, type=EntityType.TRACK, source=self.name),
            source_retriever=source,
            metadata=_make_track(track_id, self.name),
        )


class _Registry:
    """The enabled modules by name, as the server's module collection
    answers for them."""

    def __init__(self, modules: dict[str, InputModule]):
        self.modules = modules

    def enabled_input_module(self, name: str) -> Optional[InputModule]:
        return self.modules.get(name)


def _saved_state(tracks: list[Track], index: int = 0) -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(state=PlayerStateEnum.STOPPED, index=index),
        playback_mode=PlaybackMode(
            shuffle=False, repeat_single=False, repeat_all=False
        ),
        track_list=tracks,
    )


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "kalinka_state.json"
    monkeypatch.setattr(state_keeper, "STATE_FILE", str(path))
    return path


@pytest.fixture
def renderer():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    sim = SimRenderer(registry, pool)
    sim.connect()
    return sim


@pytest.fixture
def bus():
    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=_saved_state([])
    )
    yield bus
    bus.close()


@pytest.fixture
async def restore(renderer, bus):
    """Bring a queue up the way the server does: restore, then start it."""
    queues: list[PlayQueueImpl] = []

    async def _restore(modules: dict[str, InputModule]) -> PlayQueueImpl:
        queue = PlayQueueImpl(
            KalinkaConfig(),
            bus,
            renderer.registry,
            renderer.pool,
            sources=ModuleTrackSources(_Registry(modules)),
        )
        queues.append(queue)
        await state_keeper.restore_state(queue)
        await queue.__aenter__()
        return queue

    yield _restore
    for queue in queues:
        await queue.__aexit__(None, None, None)


async def _playing(queue: PlayQueueImpl) -> PlaybackState:
    for _ in range(200):
        state = await queue.get_playback_state()
        if state.state == PlayerStateEnum.PLAYING:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"playback never started: {state}")


def _unavailable(bus) -> list[bool]:
    return [track.unavailable for track in bus.get_snapshot().track_list]


async def test_restore_asks_no_module(state_file, restore):
    saved = [
        _make_track("a1", "localfiles"),
        _make_track("a2", "localfiles"),
        _make_track("a1", "localfiles"),
        _make_track("b1", "other"),
    ]
    state_file.write_text(_saved_state(saved, index=1).model_dump_json())
    local, other = _OlderModule("localfiles"), _OlderModule("other")

    queue = await restore({"localfiles": local, "other": other})

    assert local.asked == other.asked == []
    listed = await queue.list(0, 10)
    assert listed.total == 4
    assert listed.items == saved
    assert (await queue.get_playback_state()).index == 1


async def test_a_queue_restored_before_its_module_is_ready_plays_once_it_is(
    state_file, restore, bus
):
    saved = [_make_track(track_id, "qobuz") for track_id in ("a", "b", "c")]
    state_file.write_text(_saved_state(saved, index=1).model_dump_json())
    qobuz = _OlderModule("qobuz", ready=False)

    queue = await restore({"qobuz": qobuz})

    assert qobuz.asked == []
    listed = await queue.list(0, 10)
    assert listed.total == 3
    assert listed.items == saved
    assert _unavailable(bus) == [False, False, False], "only a play can tell"

    qobuz.ready = True
    await queue.play()
    state = await _playing(queue)

    assert state.index == 1
    assert state.current_track == saved[1]
    assert state.stream_url == _url("b")
    assert qobuz.asked == [["b"]]


async def test_a_track_skipped_while_its_module_started_plays_on_a_later_try(
    state_file, restore, bus
):
    saved = [_make_track("a", "qobuz"), _make_track("c", "localfiles")]
    state_file.write_text(_saved_state(saved).model_dump_json())
    qobuz = _OlderModule("qobuz", ready=False)
    queue = await restore({"qobuz": qobuz, "localfiles": _OlderModule("localfiles")})

    await queue.play()
    assert (await _playing(queue)).index == 1
    assert _unavailable(bus) == [True, False]

    qobuz.ready = True
    await queue.play(0)
    for _ in range(200):
        state = await queue.get_playback_state()
        if state.stream_url == _url("a"):
            break
        await asyncio.sleep(0.01)

    assert state.index == 0
    assert state.stream_url == _url("a")
    assert _unavailable(bus) == [False, False]


async def test_a_module_that_never_recovers_keeps_its_tracks(
    state_file, restore, bus
):
    saved = [
        _make_track("a", "qobuz"),
        _make_track("b", "qobuz"),
        _make_track("c", "localfiles"),
    ]
    state_file.write_text(_saved_state(saved).model_dump_json())

    queue = await restore(
        {
            "qobuz": _OlderModule("qobuz", ready=False),
            "localfiles": _OlderModule("localfiles"),
        }
    )
    await queue.play()
    state = await _playing(queue)

    assert state.index == 2
    assert _unavailable(bus) == [True, True, False]
    assert (await queue.list(0, 10)).total == 3

    await state_keeper.save_state(bus)
    resaved = PlayQueueState.model_validate_json(state_file.read_text())
    assert [track.id for track in resaved.track_list] == [track.id for track in saved]


async def test_a_track_without_its_module_stays_and_is_skipped(
    state_file, restore, bus
):
    """A module that is disabled or not installed keeps its tracks too."""
    saved = [_make_track("a", "qobuz"), _make_track("c", "localfiles")]
    state_file.write_text(_saved_state(saved).model_dump_json())

    queue = await restore({"localfiles": _OlderModule("localfiles")})
    await queue.play()
    state = await _playing(queue)

    assert state.index == 1
    assert _unavailable(bus) == [True, False]
    assert (await queue.list(0, 10)).total == 2


async def test_a_track_without_its_module_shows_unavailable_from_the_start(
    state_file, restore, bus
):
    saved = [_make_track("a", "qobuz"), _make_track("c", "localfiles")]
    state_file.write_text(_saved_state(saved).model_dump_json())

    queue = await restore({"localfiles": _OlderModule("localfiles")})

    assert _unavailable(bus) == [True, False]
    assert queue._unavailable_indices == {0}


async def test_a_track_flagged_for_a_missing_module_plays_once_it_is_there(
    state_file, restore, bus
):
    """The flag shows what is known now; it never keeps a track from playing."""
    state_file.write_text(_saved_state([_make_track("a", "qobuz")]).model_dump_json())
    modules: dict[str, InputModule] = {}
    queue = await restore(modules)
    assert _unavailable(bus) == [True]

    modules["qobuz"] = _OlderModule("qobuz")
    await queue.play()
    state = await _playing(queue)

    assert state.stream_url == _url("a")
    assert _unavailable(bus) == [False]


async def test_a_track_gone_from_its_module_stays_and_is_skipped(
    state_file, restore, bus
):
    saved = [_make_track("gone", "localfiles"), _make_track("kept", "localfiles")]
    state_file.write_text(_saved_state(saved).model_dump_json())

    queue = await restore({"localfiles": _OlderModule("localfiles", knows={"kept"})})
    await queue.play()
    state = await _playing(queue)

    assert state.index == 1
    assert state.stream_url == _url("kept")
    assert _unavailable(bus) == [True, False]
    assert (await queue.list(0, 10)).total == 2


async def test_a_lookup_that_overruns_is_cut_short(
    state_file, restore, bus, monkeypatch
):
    monkeypatch.setattr("kalinka_server.playqueue.SOURCE_RETRIEVAL_TIMEOUT_S", 0.2)
    saved = [_make_track("a", "jamendo"), _make_track("c", "localfiles")]
    state_file.write_text(_saved_state(saved).model_dump_json())

    queue = await restore(
        {
            "jamendo": _OlderModule("jamendo", delay_s=30),
            "localfiles": _OlderModule("localfiles"),
        }
    )
    await queue.play()
    state = await _playing(queue)

    assert state.index == 1
    assert _unavailable(bus) == [True, False]


async def test_a_track_flagged_in_the_last_run_is_restored_unflagged(
    state_file, restore, bus
):
    """The flag is set by a failed play in this run and cleared by the next
    good one; a saved flag nothing here set would never be cleared."""
    flagged = _make_track("a", "qobuz").model_copy(update={"unavailable": True})
    state_file.write_text(_saved_state([flagged]).model_dump_json())

    queue = await restore({"qobuz": _OlderModule("qobuz")})

    assert [track.unavailable for track in (await queue.list(0, 10)).items] == [
        False
    ]
    await queue.play()
    await _playing(queue)
    assert _unavailable(bus) == [False]


async def test_an_older_callers_retriever_is_not_used(renderer, bus):
    saved = [_make_track("a1", "jamendo"), _make_track("a2", "jamendo")]
    queue = PlayQueueImpl(
        KalinkaConfig(),
        bus,
        renderer.registry,
        renderer.pool,
        sources=ModuleTrackSources(_Registry({})),
    )

    async def retriever(entity_id):
        raise AssertionError("restore must not look a track up")

    await queue.restore_from_state(_saved_state(saved), retriever)

    assert (await queue.list(0, 10)).items == saved
    await queue.__aexit__(None, None, None)


def test_restore_from_state_is_not_queue_wrapped():
    # serialised keeps async methods async; restore should stay undecorated.
    assert inspect.iscoroutinefunction(PlayQueueImpl.restore_from_state)
    assert inspect.iscoroutinefunction(PlayQueueImpl.add)
    assert not hasattr(PlayQueueImpl.restore_from_state, "__wrapped__")
    assert hasattr(PlayQueueImpl.add, "__wrapped__")
