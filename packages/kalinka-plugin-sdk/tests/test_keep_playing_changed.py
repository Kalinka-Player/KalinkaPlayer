"""Whether Keep playing is on, as the replay state records it."""

from kalinka_plugin_sdk import KeepPlayingChangedEvent, PlayQueueEventType, PlayQueueState
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState


def _state() -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(),
        track_list=[],
        playback_mode=PlaybackMode(shuffle=False, repeat_single=False, repeat_all=False),
    )


def test_a_state_says_nothing_of_keep_playing_until_told():
    assert _state().keep_playing is None


def test_the_setting_is_folded_into_the_state():
    state = _state().apply(KeepPlayingChangedEvent(enabled=False, seq=1))

    assert state.keep_playing is False
    assert state.apply(KeepPlayingChangedEvent(enabled=True, seq=2)).keep_playing is True


def test_the_wire_names_the_event_and_the_switch():
    wire = KeepPlayingChangedEvent(enabled=True).model_dump(mode="json")

    assert wire["event_type"] == PlayQueueEventType.KeepPlayingChanged.value == "keep_playing_changed"
    assert wire["enabled"] is True
