import time
import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, call

from kalinka_plugin_sdk.datamodel import (
    AudioInfo,
    DeviceAccess,
    OutputInfo,
    PlaybackState,
    PlayerStateEnum,
    Album,
    EntityId,
    EntityType,
)
from kalinka_plugin_sdk.inputmodule import (
    DirectUrl,
    SourceUnavailableError,
    Track,
    TrackInfo,
    TrackSource,
)
from kalinka_plugin_sdk import (
    PlayQueueEventType,
    PlaybackStateChangedEvent,
    TracksAddedEvent,
    TracksRemovedEvent,
    TrackMovedEvent,
    TrackUnavailableEvent,
    RequestMoreTracksEvent,
    EventEmitter,
)
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.stream_state import (
    AudioFormatInfo,
    AudioGraphNodeState,
    StreamError,
    StreamErrorSource,
    StreamInfo,
    StreamState,
)
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool

from tests.fake_track_sources import FakeTrackSources
from tests.sim_renderer import (
    BITS_PER_SAMPLE,
    CHANNELS,
    DURATION_MS,
    SAMPLE_RATE,
    SimRenderer,
)

# What the simulator plays: an exclusive device running the decoded format.
AUDIO_INFO = AudioInfo(
    sample_rate=SAMPLE_RATE,
    bits_per_sample=BITS_PER_SAMPLE,
    channels=CHANNELS,
    duration_ms=DURATION_MS,
    output=OutputInfo(
        sample_rate=SAMPLE_RATE,
        bits_per_sample=BITS_PER_SAMPLE,
        channels=CHANNELS,
        access=DeviceAccess.EXCLUSIVE,
        lossless_path=True,
    ),
)


def to_track_id(id: str):
    return EntityId(
        id=id,
        type=EntityType.TRACK,
        source="test_source",
    )


def create_track(id: str):
    return Track(
        id=to_track_id(id),
        title="track" + id,
        duration=10,
        album=Album(id=to_track_id("1"), title="album1"),
    )


URL1 = "https://getsamplefiles.com/download/flac/sample-3.flac"
URL2 = "https://getsamplefiles.com/download/flac/sample-4.flac"
URL3 = "https://getsamplefiles.com/download/flac/sample-2.flac"


async def url1():
    return TrackSource(source=DirectUrl(url=URL1), format="FLAC")


async def url2():
    return TrackSource(source=DirectUrl(url=URL2), format="FLAC")


async def url3():
    return TrackSource(source=DirectUrl(url=URL3), format="FLAC")


def player_state_converter(*args, **kwargs):
    if args[0] == PlayQueueEventType.PlaybackStateChanged:
        return PlaybackState(**args[1])
    return args[1]


@pytest.fixture
def event_emitter():
    yield Mock(spec=EventEmitter)


@pytest.fixture
def config():
    config = KalinkaConfig()
    return config


@pytest.fixture
def renderer():
    """A simulated renderer behind a real registry and session pool."""
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    sim = SimRenderer(registry, pool)
    sim.connect()
    return sim


@pytest.fixture
def sources():
    """Every track plays from URL1 unless a test serves it otherwise."""
    return FakeTrackSources(default=lambda _track_id: url1())


@pytest.fixture
async def playqueue(config, event_emitter, renderer, sources):
    pq = PlayQueueImpl(
        config, event_emitter, renderer.registry, renderer.pool, sources=sources
    )
    await pq.__aenter__()

    yield pq

    await pq.__aexit__(None, None, None)

    del pq


def assert_call_args(actual_args, expected_args, position):
    for actual, expected in zip(actual_args, expected_args):
        if isinstance(expected, PlaybackStateChangedEvent):
            # Compare PlaybackStateChangedEvent payloads
            assert isinstance(
                actual, PlaybackStateChangedEvent
            ), f"at position {position} expected PlaybackStateChangedEvent but got {type(actual)}"
            actual_state = actual.state
            expected_state = expected.state
            # ignore timestamp and position for PlaybackState comparisons
            actual_state.timestamp_ns = expected_state.timestamp_ns
            if (
                actual_state.position is not None
                and expected_state.position is not None
            ):
                actual_state.position = expected_state.position
            assert (
                actual_state == expected_state
            ), f"at position {position} actual: {actual_args}\nexpected: {expected_args}"
        elif isinstance(expected, TracksAddedEvent):
            # Compare TracksAddedEvent payloads
            assert isinstance(
                actual, TracksAddedEvent
            ), f"at position {position} expected TracksAddedEvent but got {type(actual)}"
            assert (
                actual.tracks == expected.tracks
            ), f"at position {position} actual: {actual_args}\nexpected: {expected_args}"
            assert (
                actual.index == expected.index
            ), f"at position {position} actual index={actual.index} expected index={expected.index}"
        elif isinstance(expected, TracksRemovedEvent):
            # Compare TracksRemovedEvent payloads
            assert isinstance(
                actual, TracksRemovedEvent
            ), f"at position {position} expected TracksRemovedEvent but got {type(actual)}"
            assert (
                actual.indices == expected.indices
            ), f"at position {position} actual: {actual_args}\nexpected: {expected_args}"
        elif isinstance(expected, RequestMoreTracksEvent):
            # Compare RequestMoreTracksEvent payloads
            assert isinstance(
                actual, RequestMoreTracksEvent
            ), f"at position {position} expected RequestMoreTracksEvent but got {type(actual)}"
        else:
            assert (
                actual == expected
            ), f"at position {position} actual: {actual_args}\nexpected: {expected_args}"


def _extract_event_sequence(calls):
    events = []
    for event_call in calls:
        if not event_call[0] == "dispatch":
            continue
        if not event_call[1]:
            continue
        event = event_call[1][0]
        state_name = "-"
        if isinstance(event, PlaybackStateChangedEvent):
            state = event.state.state
            if isinstance(state, PlayerStateEnum):
                state_name = state.name
        events.append((type(event).__name__, state_name))
    return events


def _format_state_diff_table(expected_events, actual_events):
    max_len = max(len(expected_events), len(actual_events), 1)
    idx_width = max(3, len(str(max_len - 1)))
    expected_type_width = max(
        13, max((len(event_type) for event_type, _ in expected_events), default=0)
    )
    expected_state_width = max(
        14, max((len(state) for _, state in expected_events), default=0)
    )
    actual_type_width = max(
        11, max((len(event_type) for event_type, _ in actual_events), default=0)
    )
    actual_state_width = max(
        12, max((len(state) for _, state in actual_events), default=0)
    )

    header = (
        f"{'idx':<{idx_width}} | "
        f"{'expected event':<{expected_type_width}} | "
        f"{'expected state':<{expected_state_width}} | "
        f"{'actual event':<{actual_type_width}} | "
        f"{'actual state':<{actual_state_width}} | match"
    )
    sep = (
        f"{'-' * idx_width}-+-"
        f"{'-' * expected_type_width}-+-"
        f"{'-' * expected_state_width}-+-"
        f"{'-' * actual_type_width}-+-"
        f"{'-' * actual_state_width}-+------"
    )
    rows = [header, sep]

    for i in range(max_len):
        expected_type, expected_state = (
            expected_events[i] if i < len(expected_events) else ("-", "-")
        )
        actual_type, actual_state = (
            actual_events[i] if i < len(actual_events) else ("-", "-")
        )
        match = (
            "yes"
            if (expected_type, expected_state) == (actual_type, actual_state)
            else "no"
        )
        rows.append(
            f"{i:<{idx_width}} | "
            f"{expected_type:<{expected_type_width}} | "
            f"{expected_state:<{expected_state_width}} | "
            f"{actual_type:<{actual_type_width}} | "
            f"{actual_state:<{actual_state_width}} | {match}"
        )

    return "\n".join(rows)


def assert_has_calls(event_emitter, expected_calls):
    i = 0
    try:
        assert len(event_emitter.mock_calls) == len(expected_calls)
        for actual_call, expected_call in zip(event_emitter.mock_calls, expected_calls):
            assert actual_call[0] == expected_call[0]
            assert_call_args(actual_call[1], expected_call[1], i)
            i += 1
    except AssertionError:
        expected_events = _extract_event_sequence(expected_calls)
        actual_events = _extract_event_sequence(event_emitter.mock_calls)
        table = _format_state_diff_table(expected_events, actual_events)
        print("\nPlayback state mutations (expected vs actual):")
        print(table)
        raise


@pytest.mark.asyncio
async def test_add_remove_track(event_emitter, playqueue):
    track = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=url1
    )
    await playqueue.add([track])
    await playqueue.remove([0])
    await asyncio.sleep(0.2)
    expected_calls = [
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(state=PlayerStateEnum.STOPPED, index=0, position=0)
            )
        ),
        call.dispatch(
            TracksAddedEvent(tracks=[track.metadata] if track.metadata else [], index=0)
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                )
            )
        ),
        call.dispatch(TracksRemovedEvent(indices=[0])),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(state=PlayerStateEnum.STOPPED, index=0, position=0)
            )
        ),
    ]

    assert_has_calls(event_emitter, expected_calls)


@pytest.mark.asyncio
async def test_play(event_emitter, playqueue):
    track = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=url1
    )
    await playqueue.add([track])
    await playqueue.play()
    await asyncio.sleep(0.2)
    expected_calls = [
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED, index=0, position=0, timestamp_ns=1
                )
            )
        ),
        call.dispatch(
            TracksAddedEvent(tracks=[track.metadata] if track.metadata else [], index=0)
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(RequestMoreTracksEvent()),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
    ]
    assert_has_calls(event_emitter, expected_calls)


@pytest.mark.asyncio
async def test_switch_track(event_emitter, playqueue, sources):
    track1 = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=url1
    )
    track2 = TrackInfo(
        id=to_track_id("2"), metadata=create_track("2"), source_retriever=url2
    )
    await playqueue.add(sources.serving(track1, track2))
    await playqueue.play(0)
    await asyncio.sleep(0.2)
    await playqueue.play(1)
    await asyncio.sleep(0.2)
    expected_calls = [
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            TracksAddedEvent(tracks=[track1.metadata, track2.metadata], index=0)  # type: ignore
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track1.metadata,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track1.metadata,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=0,
                    current_track=track1.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(RequestMoreTracksEvent()),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=1,
                    position=0,
                    current_track=track2.metadata,
                    # Nothing decoded for this source yet, so no format.
                    mime_type="FLAC",
                    stream_url=URL2,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=1,
                    position=0,
                    current_track=track2.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL2,
                    timestamp_ns=1,
                )
            )
        ),
    ]
    assert_has_calls(event_emitter, expected_calls)


@pytest.mark.asyncio
async def test_play_next(event_emitter, playqueue, sources):
    track1 = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=url1
    )
    track2 = TrackInfo(
        id=to_track_id("2"), metadata=create_track("2"), source_retriever=url2
    )
    track3 = TrackInfo(
        id=to_track_id("3"), metadata=create_track("3"), source_retriever=url3
    )
    await playqueue.add(sources.serving(track1, track2, track3))
    await playqueue.play(0)
    await asyncio.sleep(0.2)
    await playqueue.play_next(1)
    await asyncio.sleep(0.2)
    await playqueue.play(2)
    await asyncio.sleep(0.2)
    expected_calls = [
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            TracksAddedEvent(
                tracks=[track1.metadata, track2.metadata, track3.metadata],  # type: ignore
                index=0,
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track1.metadata,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track1.metadata,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=0,
                    current_track=track1.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(RequestMoreTracksEvent()),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=2,
                    position=0,
                    current_track=track3.metadata,
                    # Nothing decoded for this source yet, so no format.
                    mime_type="FLAC",
                    stream_url=URL3,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=2,
                    position=0,
                    current_track=track3.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL3,
                    timestamp_ns=1,
                )
            )
        ),
    ]
    assert_has_calls(event_emitter, expected_calls)


@pytest.mark.asyncio
async def test_play_pause_stop_play(event_emitter, playqueue):
    track = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=url1
    )
    await asyncio.sleep(0.2)
    await playqueue.add([track])
    await playqueue.play()
    await asyncio.sleep(0.2)
    await playqueue.pause(True)
    await asyncio.sleep(0.2)
    await playqueue.stop()
    await asyncio.sleep(0.2)
    await playqueue.play()
    await asyncio.sleep(0.2)
    expected_calls = [
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(TracksAddedEvent(tracks=[track.metadata], index=0)),  # type: ignore
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(RequestMoreTracksEvent()),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PAUSED,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    # The renderer is holding nothing after a stop, so there is
                    # no URL to report; the format is the track's, and stays.
                    mime_type="FLAC",
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(RequestMoreTracksEvent()),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
    ]
    assert_has_calls(event_emitter, expected_calls)


@pytest.mark.asyncio
async def test_seek(event_emitter, playqueue):
    track = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=url1
    )
    await asyncio.sleep(0.2)
    await playqueue.add([track])
    await playqueue.play()
    await asyncio.sleep(0.2)
    await playqueue.seek(3000)
    await asyncio.sleep(0.2)
    expected_calls = [
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(TracksAddedEvent(tracks=[track.metadata], index=0)),  # type: ignore
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.STOPPED,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(RequestMoreTracksEvent()),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=3000,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.BUFFERING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
        call.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(
                    state=PlayerStateEnum.PLAYING,
                    index=0,
                    position=0,
                    current_track=track.metadata,
                    audio_info=AUDIO_INFO,
                    mime_type="FLAC",
                    stream_url=URL1,
                    timestamp_ns=1,
                )
            )
        ),
    ]
    assert_has_calls(event_emitter, expected_calls)


# ── Move track tests ──────────────────────────────────────────────────────────


def make_tracks(n: int) -> list[Track]:
    """Create n tracks with distinct ids."""
    return [create_track(str(i)) for i in range(1, n + 1)]


def dispatched_events(mock_emitter) -> list:
    """Return the list of events passed to event_emitter.dispatch()."""
    return [c[0][0] for c in mock_emitter.dispatch.call_args_list]


@pytest.mark.asyncio
async def test_move_currently_playing_track_forward(event_emitter, playqueue):
    """Moving the current track forward updates current_track_id and emits PlaybackStateChangedEvent."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    event_emitter.reset_mock()

    await playqueue.move(1, 3)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TrackMovedEvent)
    assert events[0].from_index == 1
    assert events[0].to_index == 3
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 3
    assert playqueue.current_track_id == 3


@pytest.mark.asyncio
async def test_move_currently_playing_track_backward(event_emitter, playqueue):
    """Moving the current track backward updates current_track_id and emits PlaybackStateChangedEvent."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 2
    event_emitter.reset_mock()

    await playqueue.move(2, 0)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TrackMovedEvent)
    assert events[0].from_index == 2
    assert events[0].to_index == 0
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 0
    assert playqueue.current_track_id == 0


@pytest.mark.asyncio
async def test_move_non_current_track_before_current_to_after(event_emitter, playqueue):
    """Moving a track before the current track to after it shifts current_track_id left."""
    # Queue: [0,1,2,3], current=2. Move 0→3: [1,2,3,0]. current should become 1.
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 2
    event_emitter.reset_mock()

    await playqueue.move(0, 3)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TrackMovedEvent)
    assert events[0].from_index == 0
    assert events[0].to_index == 3
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 1
    assert playqueue.current_track_id == 1


@pytest.mark.asyncio
async def test_move_non_current_track_after_current_to_before(event_emitter, playqueue):
    """Moving a track after the current track to before it shifts current_track_id right."""
    # Queue: [0,1,2,3], current=1. Move 3→0: [3,0,1,2]. current should become 2.
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    event_emitter.reset_mock()

    await playqueue.move(3, 0)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TrackMovedEvent)
    assert events[0].from_index == 3
    assert events[0].to_index == 0
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 2
    assert playqueue.current_track_id == 2


@pytest.mark.asyncio
async def test_retry_happens_only_for_http_stream_errors(playqueue):
    playqueue._retry_attempted = False
    playqueue._retry_current_track_async = AsyncMock()

    http_error_state = StreamState(
        state=AudioGraphNodeState.ERROR,
        error=StreamError(
            source=StreamErrorSource.HTTP_STREAM,
            message="temporary http failure",
        ),
        position=1234,
    )

    await playqueue._process_state_update(http_error_state)

    assert playqueue._retry_attempted is True
    playqueue._retry_current_track_async.assert_called_once_with(1234)


@pytest.mark.asyncio
async def test_retry_is_skipped_for_non_http_errors(playqueue):
    playqueue._retry_attempted = False
    playqueue._retry_current_track_async = AsyncMock()

    non_http_error_state = StreamState(
        state=AudioGraphNodeState.ERROR,
        error=StreamError(
            source=StreamErrorSource.AUDIO_OUTPUT,
            message="device output failure",
        ),
        position=50,
        timestamp=time.monotonic_ns(),
    )

    await playqueue._process_state_update(non_http_error_state)

    assert playqueue._retry_attempted is False
    playqueue._retry_current_track_async.assert_not_called()


def _finished_state():
    return StreamState(
        state=AudioGraphNodeState.FINISHED,
        position=1000,
        timestamp=time.monotonic_ns(),
    )


def _source_changed(stream_id=None):
    return StreamState(
        state=AudioGraphNodeState.SOURCE_CHANGED,
        timestamp=time.monotonic_ns(),
        stream_id=stream_id,
    )


def _prepare(playqueue, index, stream_id):
    playqueue.prepared_tracks[index] = (
        TrackSource(source=DirectUrl(url=f"http://example.com/t{index}.flac"), format="FLAC"),
        stream_id,
    )


@pytest.mark.asyncio
async def test_source_changed_adopts_the_stream_it_names(playqueue):
    """Two streams are queued and the renderer switches to the second — it
    skipped the first, so following the queue order would mislabel the track."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    _prepare(playqueue, 1, 41)
    _prepare(playqueue, 2, 42)

    await playqueue._process_state_update(_source_changed(42))

    assert playqueue.current_track_id == 2
    assert playqueue.current_stream_id == 42
    assert playqueue.prepared_tracks == {}  # 41 was skipped, not still pending


@pytest.mark.asyncio
async def test_source_changed_naming_the_playing_stream_changes_nothing(playqueue):
    """A repeated or reconstructed SOURCE_CHANGED must not consume the stream
    lined up behind the one already playing."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    playqueue.current_stream_id = 40
    _prepare(playqueue, 1, 41)

    await playqueue._process_state_update(_source_changed(40))

    assert playqueue.current_track_id == 0
    assert playqueue.current_stream_id == 40
    assert 1 in playqueue.prepared_tracks


@pytest.mark.asyncio
async def test_an_unnamed_source_change_still_follows_the_queue(playqueue):
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    _prepare(playqueue, 1, 41)

    await playqueue._process_state_update(_source_changed(None))

    assert playqueue.current_track_id == 1
    assert playqueue.current_stream_id == 41


@pytest.mark.asyncio
async def test_teardown_finished_does_not_autoadvance(playqueue):
    """A FINISHED emitted by tearing the graph down (clear()/clear_all()
    disconnects the last node) must NOT auto-advance to current_track_id + 1.

    Regression: clearing then immediately re-adding a queue used to play index 1
    ("the next track") because the teardown FINISHED was processed after the new
    tracks were added, with current_track_id reset to 0. The guard is that there
    is no live current stream (current_stream_id is None) during a teardown.
    """
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    playqueue.current_stream_id = None  # teardown: nothing is playing
    playqueue.prepared_tracks.clear()
    playqueue._begin_resolution = Mock()

    await playqueue._process_state_update(_finished_state())

    playqueue._begin_resolution.assert_not_called()
    assert playqueue.current_track_id == 0


@pytest.mark.asyncio
async def test_natural_finished_autoadvances(playqueue):
    """A FINISHED from a track running to its end (live current stream, no
    prefetched next) still auto-advances to the next index."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    playqueue.current_stream_id = 123  # a live, playing stream just finished
    playqueue.prepared_tracks.clear()
    playqueue._begin_resolution = Mock()

    await playqueue._process_state_update(_finished_state())

    playqueue._begin_resolution.assert_called_once()
    assert playqueue._begin_resolution.call_args.args[0] == 1


@pytest.mark.asyncio
async def test_clear_while_playing_does_not_autoadvance(event_emitter, playqueue):
    """End-to-end: clear() while a stream is current must not leave a resolution
    in flight that would start the next track."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    playqueue.current_stream_id = 123

    await playqueue.clear()
    # The teardown FINISHED lands on the interrupt lane after clear() returns.
    await playqueue._process_state_update(_finished_state())

    assert playqueue.track_list == []
    assert playqueue.current_track_id == 0
    assert not playqueue._resolution.active


@pytest.mark.asyncio
async def test_move_unrelated_tracks_no_state_change_event(event_emitter, playqueue):
    """Moving tracks that don't affect the current index emits only TrackMovedEvent."""
    # Queue: [0,1,2,3], current=0. Move 2→3: current stays at 0.
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 0
    event_emitter.reset_mock()

    await playqueue.move(2, 3)

    events = dispatched_events(event_emitter)
    assert len(events) == 1
    assert isinstance(events[0], TrackMovedEvent)
    assert events[0].from_index == 2
    assert events[0].to_index == 3
    assert playqueue.current_track_id == 0


@pytest.mark.asyncio
async def test_move_invalid_indices_no_events(event_emitter, playqueue):
    """Out-of-bounds move calls are silently ignored and emit no events."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    event_emitter.reset_mock()

    await playqueue.move(0, 99)
    await playqueue.move(99, 0)

    event_emitter.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_move_invalidates_prefetched_next_track(event_emitter, playqueue):
    """When a move makes the prefetched next track wrong, it is removed and re-prefetch is triggered.

    Scenario: [T0,T1,T2,T3], current=1, prefetch={1:url_T1, 2:url_T2}.
    Move T2 from 2→0 → [T2,T0,T1,T3].
    After remap: current→2, prepared={2:url_T1, 0:url_T2}, expected_next=3.
    Entry 0 is stale → must be evicted and re-prefetch scheduled.
    """
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    playqueue.prepared_tracks[1] = (
        TrackSource(source=DirectUrl(url="http://example.com/t1.flac"), format="FLAC"),
        0,
    )
    playqueue.prepared_tracks[2] = (
        TrackSource(source=DirectUrl(url="http://example.com/t2.flac"), format="FLAC"),
        1,
    )
    event_emitter.reset_mock()

    # move T2 (index 2) before the current track → next slot becomes wrong
    await playqueue.move(2, 0)

    # stale prefetch entry at remapped index 0 must be gone
    assert 0 not in playqueue.prepared_tracks
    # current track entry (remapped to 2) must be kept
    assert 2 in playqueue.prepared_tracks
    # a re-prefetch task must have been scheduled
    assert playqueue._prefetch_task is not None


@pytest.mark.asyncio
async def test_move_keeps_valid_prefetched_next_track(event_emitter, playqueue):
    """When a move keeps the prefetched next track correct, it is preserved with no re-prefetch.

    Scenario: [T0,T1,T2,T3], current=1, prefetch={1:url_T1, 2:url_T2}.
    Move T3 from 3→0 → [T3,T0,T1,T2].
    After remap: current→2, prepared={2:url_T1, 3:url_T2}, expected_next=3.
    Entry 3 matches → keep both, no re-prefetch.
    """
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    playqueue.prepared_tracks[1] = (
        TrackSource(source=DirectUrl(url="http://example.com/t1.flac"), format="FLAC"),
        0,
    )
    playqueue.prepared_tracks[2] = (
        TrackSource(source=DirectUrl(url="http://example.com/t2.flac"), format="FLAC"),
        1,
    )
    event_emitter.reset_mock()

    # move T3 (index 3) to position 0 — an unrelated track, next slot stays correct
    await playqueue.move(3, 0)

    # both entries must survive (remapped to new indices)
    assert 2 in playqueue.prepared_tracks  # current track (remapped 1→2)
    assert 3 in playqueue.prepared_tracks  # next track (remapped 2→3, still correct)
    assert len(playqueue.prepared_tracks) == 2
    # no re-prefetch should have been triggered
    assert playqueue._prefetch_task is None


# ── Insert (add with index) tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_with_no_index_appends(event_emitter, playqueue):
    """add(tracks) with no index appends; TracksAddedEvent.index == original length."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    event_emitter.reset_mock()

    new_track = make_tracks(1)
    await playqueue.add(new_track)

    events = dispatched_events(event_emitter)
    assert len(events) == 1
    assert isinstance(events[0], TracksAddedEvent)
    assert events[0].index == 3
    assert len(playqueue.track_list) == 4
    assert playqueue.current_track_id == 0


@pytest.mark.asyncio
async def test_add_insert_at_end_explicitly(event_emitter, playqueue):
    """add(tracks, index=len) is identical to appending."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    event_emitter.reset_mock()

    new_track = make_tracks(1)
    await playqueue.add(new_track, index=3)

    events = dispatched_events(event_emitter)
    assert len(events) == 1
    assert isinstance(events[0], TracksAddedEvent)
    assert events[0].index == 3
    assert len(playqueue.track_list) == 4
    assert playqueue.current_track_id == 0


@pytest.mark.asyncio
async def test_add_insert_after_current_not_at_next(event_emitter, playqueue):
    """Inserting after current+1 emits TracksAddedEvent only; current unchanged."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    event_emitter.reset_mock()

    # insert at 3, which is > current+1=2
    new_track = make_tracks(1)
    await playqueue.add(new_track, index=3)

    events = dispatched_events(event_emitter)
    assert len(events) == 1
    assert isinstance(events[0], TracksAddedEvent)
    assert events[0].index == 3
    assert playqueue.current_track_id == 1
    assert len(playqueue.track_list) == 5


@pytest.mark.asyncio
async def test_add_insert_at_next_slot_invalidates_prefetch(event_emitter, playqueue):
    """Inserting exactly at current+1 evicts stale prefetch and schedules re-prefetch."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    # Simulate a prefetched next track at index 2
    playqueue.prepared_tracks[2] = (
        TrackSource(source=DirectUrl(url="http://example.com/t2.flac"), format="FLAC"),
        42,
    )
    event_emitter.reset_mock()

    # Insert at current+1=2 — the new track displaces the prefetched one
    new_track = make_tracks(1)
    await playqueue.add(new_track, index=2)

    # Stale prefetch (shifted to 3) must be evicted
    assert 3 not in playqueue.prepared_tracks
    # A re-prefetch task must have been scheduled
    assert playqueue._prefetch_task is not None
    assert len(playqueue.track_list) == 5
    assert playqueue.current_track_id == 1


@pytest.mark.asyncio
async def test_add_insert_before_current(event_emitter, playqueue):
    """Inserting before current shifts current_track_id right and emits PlaybackStateChangedEvent."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 2
    event_emitter.reset_mock()

    new_track = make_tracks(1)
    await playqueue.add(new_track, index=1)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TracksAddedEvent)
    assert events[0].index == 1
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 3
    assert playqueue.current_track_id == 3
    assert len(playqueue.track_list) == 5


@pytest.mark.asyncio
async def test_add_insert_at_current_position(event_emitter, playqueue):
    """Inserting at current index shifts current right (new track slides in before it)."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 2
    event_emitter.reset_mock()

    new_track = make_tracks(1)
    await playqueue.add(new_track, index=2)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TracksAddedEvent)
    assert events[0].index == 2
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 3
    assert playqueue.current_track_id == 3


@pytest.mark.asyncio
async def test_add_insert_multiple_tracks_before_current(event_emitter, playqueue):
    """Inserting N tracks before current shifts current_track_id by N."""
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 2
    event_emitter.reset_mock()

    new_tracks = make_tracks(3)
    await playqueue.add(new_tracks, index=0)

    events = dispatched_events(event_emitter)
    assert len(events) == 2
    assert isinstance(events[0], TracksAddedEvent)
    assert events[0].index == 0
    assert len(events[0].tracks) == 3
    assert isinstance(events[1], PlaybackStateChangedEvent)
    assert events[1].state.index == 5
    assert playqueue.current_track_id == 5


@pytest.mark.asyncio
async def test_add_insert_clamped(event_emitter, playqueue):
    """Negative index is clamped to 0; index beyond len is clamped to len."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    event_emitter.reset_mock()

    # Negative → clamped to 0
    await playqueue.add(make_tracks(1), index=-5)
    events = dispatched_events(event_emitter)
    assert events[0].index == 0

    event_emitter.reset_mock()

    # Beyond len → clamped to len
    await playqueue.add(make_tracks(1), index=999)
    events = dispatched_events(event_emitter)
    assert events[0].index == 4  # len was 4 after previous insert


@pytest.mark.asyncio
async def test_add_insert_preserves_valid_prefetch(event_emitter, playqueue):
    """Inserting after the next slot keeps a valid prefetched entry intact.

    Queue: [T0,T1,T2,T3], current=1, prefetch={2: url_T2}.
    Insert at 3 (> current+1=2) → prefetch at 2 is still expected_next → keep.
    """
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue.current_track_id = 1
    playqueue.prepared_tracks[2] = (
        TrackSource(source=DirectUrl(url="http://example.com/t2.flac"), format="FLAC"),
        7,
    )
    event_emitter.reset_mock()

    await playqueue.add(make_tracks(1), index=3)

    # Prefetch at 2 must still be there (insert at 3 didn't displace it)
    assert 2 in playqueue.prepared_tracks
    # No re-prefetch triggered
    assert playqueue._prefetch_task is None
    assert len(playqueue.track_list) == 5


@pytest.mark.asyncio
async def test_a_queued_track_is_resolved_through_its_module_only_when_played(
    playqueue, sources
):
    """A TrackInfo is still accepted, as callers of SDK 3.7 hand it over, but
    only its metadata is kept: its retriever is never called."""

    async def stale():
        raise AssertionError("the queue must not use a TrackInfo's retriever")

    info = TrackInfo(
        id=to_track_id("1"), metadata=create_track("1"), source_retriever=stale
    )
    await playqueue.add([info, create_track("2")])

    assert playqueue.track_list == [info.metadata, create_track("2")]
    assert sources.resolved == []

    index, source, _, failed = await playqueue._resolve_playable(0)

    assert (index, failed) == (0, [])
    assert source.source.url == URL1
    assert sources.resolved == [to_track_id("1").to_string]


@pytest.mark.asyncio
async def test_a_track_info_without_metadata_is_not_queued(playqueue):
    await playqueue.add(
        [
            TrackInfo(id=to_track_id("1"), metadata=None, source_retriever=url1),
            create_track("2"),
        ]
    )

    assert [track.id.id for track in playqueue.track_list] == ["2"]


# ── Unavailable-track (link retrieval failure) tests ────────────────────────────


async def failing_url():
    raise KeyError("url")


def make_tracks_with_failures(
    sources: FakeTrackSources, n: int, failing: set[int]
) -> list[Track]:
    """Create n tracks; those at indices in ``failing`` raise on link
    retrieval."""
    tracks = make_tracks(n)
    for i in failing:
        sources.serve(tracks[i].id, failing_url)
    return tracks


@pytest.mark.asyncio
async def test_resolve_playable_skips_failed_track(event_emitter, playqueue, sources):
    """A track whose URL can't be fetched is collected in ``failed`` and skipped;
    the next playable track is returned. The pure resolver mutates no state and
    dispatches no events (the commit step flags failures)."""
    playqueue.track_list = make_tracks_with_failures(sources, 3, {0})
    event_emitter.reset_mock()

    index, track_url, track_ref, failed = await playqueue._resolve_playable(0, step=1)

    assert index == 1
    assert track_url is not None
    assert track_ref is playqueue.track_list[1]
    assert failed == [(0, None)]
    # Resolver is pure: no flagging, no events.
    assert playqueue._unavailable_indices == set()
    assert not any(
        isinstance(e, TrackUnavailableEvent) for e in dispatched_events(event_emitter)
    )


@pytest.mark.asyncio
async def test_resolve_playable_carries_the_module_reason(
    event_emitter, playqueue, sources
):
    """A SourceUnavailableError's message (user-presentable, e.g. an unmounted
    share) rides along with the failed index so the commit step can put it in
    the TrackUnavailableEvent."""

    async def unavailable():
        raise SourceUnavailableError("Music folder /mnt/nas is not available")

    tracks = make_tracks(2)
    sources.serve(tracks[0].id, unavailable)
    playqueue.track_list = tracks

    index, _, _, failed = await playqueue._resolve_playable(0, step=1)

    assert index == 1
    assert failed == [(0, "Music folder /mnt/nas is not available")]


@pytest.mark.asyncio
async def test_resolve_playable_refuses_a_sequential_source(
    event_emitter, playqueue, sources
):
    """The queue resumes a track at an offset, which a sequential source
    cannot do; it plays only through its plugin's direct playback."""

    async def live():
        return TrackSource(
            source=DirectUrl(url="http://example.com/live"),
            format="ogg",
            sequential=True,
        )

    tracks = make_tracks(2)
    sources.serve(tracks[0].id, live)
    playqueue.track_list = tracks

    index, _, _, failed = await playqueue._resolve_playable(0, step=1)

    assert index == 1
    assert failed == [(0, "This source plays only through its own plugin")]


@pytest.mark.asyncio
async def test_resolve_playable_all_failed_returns_none(
    event_emitter, playqueue, sources
):
    """When every candidate fails, resolve returns (None, None, all-indices)."""
    playqueue.track_list = make_tracks_with_failures(sources, 3, {0, 1, 2})
    event_emitter.reset_mock()

    index, track_url, track_ref, failed = await playqueue._resolve_playable(0, step=1)

    assert index is None
    assert track_url is None
    assert track_ref is None
    assert failed == [(0, None), (1, None), (2, None)]
    # Resolver does not mutate flag state; the commit step would.
    assert playqueue._unavailable_indices == set()


@pytest.mark.asyncio
async def test_resolve_playable_success_reports_no_failures(event_emitter, playqueue):
    """A track that resolves on the first try yields an empty ``failed`` list and
    leaves flag state untouched (clearing is the commit's job)."""
    playqueue.track_list = make_tracks(3)
    playqueue._unavailable_indices = {0}
    event_emitter.reset_mock()

    index, track_url, track_ref, failed = await playqueue._resolve_playable(0, step=1)

    assert index == 0
    assert track_url is not None
    assert track_ref is playqueue.track_list[0]
    assert failed == []
    assert playqueue._unavailable_indices == {0}
    assert not any(
        isinstance(e, TrackUnavailableEvent) for e in dispatched_events(event_emitter)
    )


@pytest.mark.asyncio
async def test_set_track_unavailable_dedups(event_emitter, playqueue):
    """Repeated marks/clears only emit an event when the state actually flips."""
    playqueue.track_list = make_tracks(3)
    event_emitter.reset_mock()

    playqueue._set_track_unavailable(1, True)
    playqueue._set_track_unavailable(1, True)
    playqueue._set_track_unavailable(1, False)
    playqueue._set_track_unavailable(1, False)

    events = [
        e for e in dispatched_events(event_emitter) if isinstance(e, TrackUnavailableEvent)
    ]
    assert events == [
        TrackUnavailableEvent(index=1, unavailable=True),
        TrackUnavailableEvent(index=1, unavailable=False),
    ]
    assert playqueue._unavailable_indices == set()


@pytest.mark.asyncio
async def test_unavailable_indices_remap_on_add(event_emitter, playqueue):
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue._unavailable_indices = {1, 2}

    await playqueue.add(make_tracks(2), index=0)
    await asyncio.sleep(0)

    assert playqueue._unavailable_indices == {3, 4}


@pytest.mark.asyncio
async def test_unavailable_indices_remap_on_remove(event_emitter, playqueue):
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue._unavailable_indices = {2, 3}

    await playqueue.remove([0])

    assert playqueue._unavailable_indices == {1, 2}


@pytest.mark.asyncio
async def test_unavailable_indices_dropped_on_remove(event_emitter, playqueue):
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue._unavailable_indices = {1, 3}

    await playqueue.remove([1])

    # Index 1 removed; index 3 shifts down to 2.
    assert playqueue._unavailable_indices == {2}


@pytest.mark.asyncio
async def test_unavailable_indices_remap_on_move(event_emitter, playqueue):
    await playqueue.add(make_tracks(4))
    await asyncio.sleep(0)
    playqueue._unavailable_indices = {0}

    await playqueue.move(0, 2)

    assert playqueue._unavailable_indices == {2}


@pytest.mark.asyncio
async def test_unavailable_indices_cleared_on_clear(event_emitter, playqueue):
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    playqueue._unavailable_indices = {0, 1}

    await playqueue.clear()

    assert playqueue._unavailable_indices == set()


@pytest.mark.asyncio
async def test_play_out_of_range_index_is_noop(event_emitter, playqueue):
    """play() with an out-of-range index does nothing (no wrap, no flagging)."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    event_emitter.reset_mock()

    await playqueue.play(99)

    assert not playqueue.prepared_tracks
    assert playqueue._unavailable_indices == set()
    assert not any(
        isinstance(e, TrackUnavailableEvent)
        for e in dispatched_events(event_emitter)
    )


@pytest.mark.asyncio
async def test_play_next_out_of_range_index_is_noop(event_emitter, playqueue):
    """play_next() with an out-of-range index does nothing."""
    await playqueue.add(make_tracks(3))
    await asyncio.sleep(0)
    event_emitter.reset_mock()

    await playqueue.play_next(99)
    await playqueue.play_next(-5)

    assert playqueue._unavailable_indices == set()
    assert not any(
        isinstance(e, TrackUnavailableEvent)
        for e in dispatched_events(event_emitter)
    )


# ── Off-lane URL resolution semantics ───────────────────────────────────────────
#
# These verify the *wiring* between PlayQueueImpl and its ResolutionSlot. They use
# a source that never resolves so resolution stays in flight and nothing ever
# commits to the player. The slot mechanics themselves are unit-tested in
# test_resolution_slot.py; end-to-end playback (commit → renderer enqueue) is
# covered by the streaming tests above.


async def _never_resolves():
    """A source that blocks until its resolution task is cancelled."""
    await asyncio.Event().wait()


def _slow_track(sources: FakeTrackSources, id: str) -> Track:
    track = create_track(id)
    sources.serve(track.id, _never_resolves)
    return track


@pytest.mark.asyncio
async def test_play_does_not_block_on_url_resolution(playqueue, sources):
    """play() returns at once while the URL is still being fetched, and the
    serial command lane stays free for other commands."""
    playqueue.track_list = [_slow_track(sources, "1")]

    await playqueue.play(0)
    assert playqueue._resolution.active  # in flight, not yet committed

    # Lane is free: another serialised call resolves promptly despite the
    # in-flight (blocked) resolution.
    state = await asyncio.wait_for(playqueue.get_playback_state(), timeout=1)
    assert state is not None


@pytest.mark.asyncio
async def test_failed_play_emits_unavailable_event(event_emitter, playqueue, sources):
    """A play that runs to completion and fails to resolve flags the track."""
    playqueue.track_list = make_tracks_with_failures(sources, 1, {0})
    event_emitter.reset_mock()

    await playqueue.play(0)
    await asyncio.sleep(0.05)

    unavailable = [
        e for e in dispatched_events(event_emitter) if isinstance(e, TrackUnavailableEvent)
    ]
    assert unavailable == [TrackUnavailableEvent(index=0, unavailable=True)]
    assert playqueue._unavailable_indices == {0}


@pytest.mark.asyncio
async def test_superseded_play_suppresses_unavailable_event(
    event_emitter, playqueue, sources
):
    """A play whose resolution is superseded by a newer play fails silently:
    no TrackUnavailableEvent for the abandoned track."""
    playqueue.track_list = [_slow_track(sources, "1"), _slow_track(sources, "2")]
    event_emitter.reset_mock()

    await playqueue.play(0)
    assert playqueue._resolution.target == 0
    await playqueue.play(1)  # supersedes the index-0 resolution
    await asyncio.sleep(0.05)

    assert playqueue._resolution.target == 1  # the newer play won
    assert not any(
        isinstance(e, TrackUnavailableEvent) and e.index == 0
        for e in dispatched_events(event_emitter)
    )
    assert 0 not in playqueue._unavailable_indices


@pytest.mark.asyncio
async def test_structural_mutation_on_target_cancels_resolution(
    event_emitter, playqueue, sources
):
    """Removing a track at/before the resolution target cancels it silently."""
    tracks = make_tracks(4)
    tracks[2] = _slow_track(sources, "slow")
    playqueue.track_list = tracks
    event_emitter.reset_mock()

    await playqueue.play(2)
    assert playqueue._resolution.active

    await playqueue.remove([0])  # index 0 <= target 2 → affected
    assert not playqueue._resolution.active  # cancelled

    await asyncio.sleep(0.05)
    assert not any(
        isinstance(e, TrackUnavailableEvent)
        for e in dispatched_events(event_emitter)
    )


@pytest.mark.asyncio
async def test_structural_mutation_after_target_keeps_resolution(playqueue, sources):
    """A removal entirely after the resolution target leaves it running."""
    tracks = make_tracks(4)
    tracks[0] = _slow_track(sources, "slow")
    playqueue.track_list = tracks

    await playqueue.play(0)
    assert playqueue._resolution.active

    await playqueue.remove([3])  # index 3 > target 0 → unaffected
    assert playqueue._resolution.active
    assert playqueue._resolution.target == 0


@pytest.mark.asyncio
async def test_accept_scan_rejects_shifted_index(playqueue):
    """The commit re-validates the resolved index against the track that was
    actually fetched. A multi-step scan can resolve an index *past* the start
    target, and a concurrent add/remove/move in the (target, resolved] band
    slips past cancel_if (whose predicate keys on the start target). _accept_scan
    must then bail rather than bind the URL to the now-different track."""
    captured = {}

    async def _record_gen(gen):
        captured["gen"] = gen

    url = TrackSource(source=DirectUrl(url="http://x/1.flac"), format="FLAC")
    tracks = make_tracks(3)
    playqueue.track_list = list(tracks)
    resolved_ref = tracks[1]  # the track whose link produced `url`

    # Arm a live resolution generation targeting index 1.
    playqueue._resolution.start(_record_gen, target=1)
    await asyncio.sleep(0)
    gen = captured["gen"]

    # Index still holds the fetched track → accepted.
    assert playqueue._accept_scan(gen, (1, url, resolved_ref, [])) == (1, url)

    # Re-arm (the accept above called finish()).
    playqueue._resolution.start(_record_gen, target=1)
    await asyncio.sleep(0)
    gen = captured["gen"]

    # A different track now sits at index 1 (a concurrent reindex) → must bail.
    playqueue.track_list[1] = make_tracks(1)[0]
    assert playqueue._accept_scan(gen, (1, url, resolved_ref, [])) is None


def test_playqueue_state_apply_track_unavailable():
    """PlayQueueState.apply toggles the per-track unavailable flag and ignores
    out-of-range indices."""
    from kalinka_plugin_sdk.datamodel import PlaybackMode
    from kalinka_plugin_sdk.events import PlayQueueState

    state = PlayQueueState(
        playback_state=PlaybackState(state=PlayerStateEnum.STOPPED, index=0),
        track_list=[create_track("1"), create_track("2")],
        playback_mode=PlaybackMode(
            shuffle=False, repeat_single=False, repeat_all=False
        ),
        seq=0,
    )

    marked = state.apply(TrackUnavailableEvent(index=1, unavailable=True, seq=1))
    assert marked.track_list[1].unavailable is True
    assert marked.track_list[0].unavailable is False

    cleared = marked.apply(TrackUnavailableEvent(index=1, unavailable=False, seq=2))
    assert cleared.track_list[1].unavailable is False

    unchanged = cleared.apply(
        TrackUnavailableEvent(index=99, unavailable=True, seq=3)
    )
    assert unchanged is cleared


def _streaming_with_duration(duration_ms):
    return StreamState(
        state=AudioGraphNodeState.STREAMING,
        timestamp=time.monotonic_ns(),
        stream_info=StreamInfo(
            format=AudioFormatInfo(sample_rate=44100, channels=2, bits_per_sample=16),
            duration_ms=duration_ms,
        ),
        stream_id=1,
    )


@pytest.mark.asyncio
async def test_a_stream_of_unknown_length_schedules_no_prefetch(playqueue):
    """Timing off an unknown length would put the prefetch moment in the past
    and advance the queue the instant playback started."""
    playqueue._setup_prefetch_timer(_streaming_with_duration(None))

    assert playqueue._prefetch_task is None


@pytest.mark.asyncio
async def test_a_known_length_still_schedules_a_prefetch(playqueue):
    playqueue._setup_prefetch_timer(_streaming_with_duration(204_000))

    assert playqueue._prefetch_task is not None
    playqueue._cancel_prefetch_timer()
