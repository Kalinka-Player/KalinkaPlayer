"""What a plugin subscribes to is on record, so unloading it closes what its
shutdown left open."""

import pytest

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState
from kalinka_plugin_sdk.events import PlayQueueEvent, PlayQueueEventType, PlayQueueState

from kalinka_server.scoped_listener import ScopedListener

TYPES = [PlayQueueEventType.PlaybackStateChanged]


@pytest.fixture
def bus():
    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    yield bus
    bus.close()


def test_what_the_plugin_unsubscribed_is_not_counted(bus):
    listener = ScopedListener(bus, "fake")
    subscription = listener.subscribe(TYPES, lambda event: None)

    listener.unsubscribe(subscription.id)

    assert listener.revoke_all() == 0


def test_what_the_plugin_closed_through_its_handle_is_not_counted(bus):
    listener = ScopedListener(bus, "fake")
    subscription = listener.subscribe(TYPES, lambda event: None)

    subscription.unsubscribe()

    assert listener.revoke_all() == 0


def test_what_it_left_open_is_closed_once(bus):
    listener = ScopedListener(bus, "fake")
    listener.subscribe(TYPES, lambda event: None)
    listener.subscribe(TYPES, lambda event: None)

    assert listener.revoke_all() == 2
    assert listener.revoke_all() == 0


async def test_a_stream_is_on_record_while_it_is_open(bus):
    listener = ScopedListener(bus, "fake")

    async with listener.stream(TYPES) as stream:
        assert await stream.__anext__() is not None
        assert len(listener._ids) == 1

    assert listener.revoke_all() == 0
