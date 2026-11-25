"""A favourite marked or unmarked, as the queue bus tells other clients."""

from kalinka_plugin_sdk import FavoriteChangedEvent, PlayQueueEventType, PlayQueueState
from kalinka_plugin_sdk.datamodel import EntityId, PlaybackMode, PlaybackState

TRACK = EntityId.from_string("kalinka:tidal:track:11")


def test_the_wire_names_the_item_and_what_it_now_is():
    wire = FavoriteChangedEvent(id=TRACK, favorite=True).model_dump(mode="json")

    assert (
        wire["event_type"]
        == PlayQueueEventType.FavoriteChanged.value
        == "favorite_changed"
    )
    assert (wire["id"], wire["favorite"]) == ("kalinka:tidal:track:11", True)


def test_a_replayed_state_keeps_no_favourites():
    state = PlayQueueState(
        playback_state=PlaybackState(),
        track_list=[],
        playback_mode=PlaybackMode(
            shuffle=False, repeat_single=False, repeat_all=False
        ),
    )

    assert state.apply(FavoriteChangedEvent(id=TRACK, favorite=True, seq=1)) is state
