"""Keep playing carries the queue on with what its sources propose, and steers away
from what the listener turned down until the queue's session ends."""

import asyncio
from types import SimpleNamespace

import pytest

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.datamodel import (
    Album,
    BrowseItem,
    EntityId,
    EntityType,
    PlaybackMode,
    PlaybackState,
    PlayerStateEnum,
    Track,
    TrackList,
)
from kalinka_plugin_sdk.events import (
    PlayQueueState,
    RequestMoreTracksEvent,
    TracksAddedEvent,
    TracksRemovedEvent,
)
from kalinka_plugin_sdk.inputmodule import ContinuationRequest, InputModule, ModuleCapability

from kalinka_server.keep_playing import KeepPlayingController, interleave, plan_continuation
from kalinka_server.config_model import QueueConfig
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.queue_limit import QueueLimit


def _track(source: str, local: str) -> Track:
    tid = EntityId(id=local, type=EntityType.TRACK, source=source)
    album = EntityId(id=f"al-{local}", type=EntityType.ALBUM, source=source)
    return Track(id=tid, title=local, duration=1, album=Album(id=album, title=""))


class _Queue:
    def __init__(self, tracks, index, state=None):
        self.tracks = list(tracks)
        self.index = index
        self.state = state
        self.adds = []
        self.skips = 0
        self.plays = []

    async def list(self, offset, limit):
        return TrackList(
            offset=offset, limit=limit, total=len(self.tracks), items=self.tracks[offset : offset + limit]
        )

    async def get_playback_state(self):
        playing = self.tracks[self.index] if self.index is not None and self.tracks else None
        return PlaybackState(state=self.state, current_track=playing, index=self.index)

    async def add(self, tracks, index=None):
        self.adds.append(([t.id.id for t in tracks], index))
        if index is None:
            self.tracks.extend(tracks)
        else:
            self.tracks[index:index] = tracks

    async def play(self, index=None):
        self.plays.append(index)

    async def next(self):
        self.skips += 1
        if self.index is not None and self.index + 1 < len(self.tracks):
            self.index += 1


class _Module(InputModule):
    def __init__(self, name, proposals=(), fails=False, continues=True):
        self.name = name
        self.proposals = list(proposals)
        self.fails = fails
        self.continues = continues
        self.requests: list[ContinuationRequest] = []

    def module_name(self):
        return self.name

    def capabilities(self):
        return frozenset({ModuleCapability.CONTINUATION}) if self.continues else frozenset()

    async def continue_queue(self, request):
        self.requests.append(request)
        if self.fails:
            raise RuntimeError("source down")
        return [p for p in self.proposals if p not in request.exclude][: request.count]

    async def get_all(self, entity_ids):
        return [
            BrowseItem(id=e, name=e.id, can_add=True, track=_track(e.source, e.id)) for e in entity_ids
        ]


def _bus():
    return EventBus(
        PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(shuffle=False, repeat_single=False, repeat_all=False),
        )
    )


def _controller(queue, modules, *, limit=1000, keep_playing=True, added=2, bus=None):
    config = QueueConfig(keep_playing=keep_playing, tracks_added=added)
    return KeepPlayingController(
        config=config,
        playqueue=queue,
        playqueue_eventbus=bus or _bus(),
        module_for=modules.get,
        queue_limit=lambda: QueueLimit(limit, {}, {}),
        adding=asyncio.Lock(),
    )


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def test_plan_asks_the_playing_tracks_source_first_with_its_own_seeds_oldest_first():
    queue = [_track("lf", "a"), _track("qobuz", "q1"), _track("lf", "b"), _track("lf", "c")]

    asks = plan_continuation(queue, current=3, dislikes={}, count=2)

    assert [a.source for a in asks] == ["lf", "qobuz"]
    assert asks[0].request.seeds == ["a", "b", "c"]
    assert asks[1].request.seeds == ["q1"]
    assert asks[0].request.exclude == ["a", "b", "c"]
    assert asks[0].request.count == 2


def test_plan_seeds_only_from_what_was_heard_and_never_from_a_turned_down_track():
    queue = [_track("lf", "a"), _track("lf", "b"), _track("lf", "upcoming")]

    (ask,) = plan_continuation(queue, current=1, dislikes={"lf": ["b"]}, count=1)

    assert ask.request.seeds == ["a"]
    assert ask.request.avoid == ["b"]
    assert set(ask.request.exclude) == {"a", "b", "upcoming"}


def test_plan_for_more_like_this_asks_only_the_playing_tracks_source():
    queue = [_track("qobuz", "q1"), _track("lf", "a"), _track("lf", "b")]

    (ask,) = plan_continuation(queue, current=1, dislikes={}, count=3, from_current_only=True)

    assert ask.source == "lf"
    assert ask.request.seeds == ["a"]


def test_plan_of_an_empty_queue_asks_nobody():
    assert plan_continuation([], current=None, dislikes={}, count=1) == []


def test_interleave_takes_turns_skipping_what_is_queued_or_taken_until_full():
    lf = [_track("lf", "x"), _track("lf", "y"), _track("lf", "z")]
    qobuz = [_track("qobuz", "q"), _track("lf", "x")]
    queued = {_track("lf", "y").id.to_string}

    taken = interleave([lf, qobuz], queued, room=3)

    assert [t.id.id for t in taken] == ["x", "q", "z"]
    assert [t.id.id for t in interleave([lf, qobuz], set(), room=1)] == ["x"]


async def test_extend_appends_from_every_source_that_continues_and_survives_one_failing():
    queue = _Queue([_track("lf", "a"), _track("qobuz", "q1"), _track("drives", "d1")], index=2)
    modules = {
        "lf": _Module("lf", ["b", "c", "d"]),
        "qobuz": _Module("qobuz", fails=True),
        "drives": _Module("drives", ["e"], continues=False),
    }

    added = await _controller(queue, modules).extend()

    assert added == 2
    assert queue.adds == [(["b", "c"], None)]
    assert modules["drives"].requests == []


async def test_extend_adds_the_amount_in_all_taking_the_playing_tracks_source_first():
    queue = _Queue([_track("qobuz", "q1"), _track("lf", "a")], index=1)
    modules = {"lf": _Module("lf", ["b", "c"]), "qobuz": _Module("qobuz", ["q2", "q3"])}

    assert await _controller(queue, modules, added=1).extend() == 1

    assert queue.adds == [(["b"], None)]
    assert all(m.requests[0].count == 1 for m in modules.values())


async def test_a_source_with_nothing_to_offer_is_covered_by_the_next():
    queue = _Queue([_track("qobuz", "q1"), _track("lf", "a")], index=1)
    modules = {"lf": _Module("lf"), "qobuz": _Module("qobuz", ["q2", "q3"])}

    assert await _controller(queue, modules, added=1).extend() == 1

    assert queue.adds == [(["q2"], None)]


async def test_a_queue_that_moved_on_since_asking_is_not_extended():
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=0)
    modules = {"lf": _Module("lf", ["x"])}

    assert await _controller(queue, modules).extend() == 0

    assert queue.adds == [] and modules["lf"].requests == []


async def test_more_like_current_queues_its_sources_tracks_right_after_it():
    queue = _Queue([_track("lf", "a"), _track("lf", "b"), _track("lf", "later")], index=1)
    modules = {"lf": _Module("lf", ["x", "y", "z"])}

    added = await _controller(queue, modules).more_like_current(count=2)

    assert added == 2
    assert queue.adds == [(["x", "y"], 2)]
    assert modules["lf"].requests[0].seeds == ["b"]
    assert "later" in modules["lf"].requests[0].exclude


async def test_extend_stops_at_the_queue_limit():
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=1)
    modules = {"lf": _Module("lf", ["x", "y"])}

    added = await _controller(queue, modules, limit=3).extend()

    assert added == 1
    assert queue.adds == [(["x"], None)]


async def test_dislike_turns_the_track_down_extends_a_queue_at_its_end_and_skips():
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=1)
    modules = {"lf": _Module("lf", ["x", "y"])}
    controller = _controller(queue, modules)

    assert await controller.dislike_current()

    assert controller.dislikes == {"lf": ["b"]}
    request = modules["lf"].requests[0]
    assert request.seeds == ["a"] and request.avoid == ["b"]
    assert queue.skips == 1
    assert queue.tracks[queue.index].id.id == "x"


async def test_dislike_with_keep_playing_off_only_skips():
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=1)
    modules = {"lf": _Module("lf", ["x"])}

    assert await _controller(queue, modules, keep_playing=False).dislike_current()

    assert queue.adds == []
    assert queue.skips == 1


async def test_dislike_with_nothing_playing_does_nothing():
    queue = _Queue([], index=None)

    assert not await _controller(queue, {}).dislike_current()
    assert queue.skips == 0


async def test_turn_downs_last_until_the_whole_queue_goes_at_once():
    bus = _bus()
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=0)
    controller = _controller(queue, {"lf": _Module("lf")}, bus=bus)
    await controller.start()
    try:
        await controller.dislike_current()
        bus.dispatch(TracksRemovedEvent(indices=[0]))
        bus.dispatch(TracksAddedEvent(tracks=[_track("lf", "c")], index=1))
        bus.dispatch(RequestMoreTracksEvent())
        await _until(lambda: len(controller._module_for("lf").requests) == 1)
        assert controller.dislikes == {"lf": ["a"]}

        bus.dispatch(TracksRemovedEvent(indices=[1, 0]))
        await _until(lambda: controller.dislikes == {})
    finally:
        await controller.shutdown()
        bus.close()


def test_the_queue_asks_for_more_only_while_keep_playing_is_on():
    dispatched = []

    def queue(keep_playing):
        return SimpleNamespace(
            _config=SimpleNamespace(queue=QueueConfig(keep_playing=keep_playing)),
            repeat_single=False,
            repeat_all=False,
            current_track_id=1,
            track_list=[object(), object()],
            event_emitter=SimpleNamespace(dispatch=dispatched.append),
        )

    PlayQueueImpl._request_more_tracks(queue(False))
    assert dispatched == []
    PlayQueueImpl._request_more_tracks(queue(True))
    assert [type(e) for e in dispatched] == [RequestMoreTracksEvent]


def test_by_default_the_queue_carries_on_a_track_at_a_time():
    config = QueueConfig()

    assert config.keep_playing and config.tracks_added == 1


@pytest.mark.parametrize("added", [0, 11])
def test_tracks_added_at_a_time_stays_in_bounds(added):
    with pytest.raises(ValueError):
        QueueConfig(tracks_added=added)


async def test_starting_tells_queue_clients_whether_it_is_on():
    bus = _bus()
    controller = _controller(_Queue([], index=None), {}, keep_playing=False, bus=bus)
    await controller.start()
    try:
        assert bus.get_snapshot().keep_playing is False
        controller._config.keep_playing = True
        controller.announce()
        assert bus.get_snapshot().keep_playing is True
    finally:
        await controller.shutdown()
        bus.close()


async def test_resuming_a_finished_queue_carries_it_on_and_plays_the_first_new_track():
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=1, state=PlayerStateEnum.STOPPED)
    modules = {"lf": _Module("lf", ["x", "y"])}

    assert await _controller(queue, modules).resume() == 2

    assert [t.id.id for t in queue.tracks] == ["a", "b", "x", "y"]
    assert queue.plays == [2]


async def test_resuming_during_the_last_track_only_carries_the_queue_on():
    queue = _Queue([_track("lf", "a")], index=0, state=PlayerStateEnum.PLAYING)
    modules = {"lf": _Module("lf", ["x"])}

    assert await _controller(queue, modules).resume() == 1

    assert queue.plays == []


async def test_resuming_with_nothing_to_offer_leaves_playback_alone():
    queue = _Queue([_track("lf", "a")], index=0, state=PlayerStateEnum.STOPPED)

    assert await _controller(queue, {"lf": _Module("lf")}).resume() == 0

    assert queue.plays == []


async def test_resuming_short_of_the_last_track_adds_nothing():
    queue = _Queue([_track("lf", "a"), _track("lf", "b")], index=0, state=PlayerStateEnum.PLAYING)
    modules = {"lf": _Module("lf", ["x"])}

    assert await _controller(queue, modules).resume() == 0

    assert queue.adds == [] and modules["lf"].requests == []
