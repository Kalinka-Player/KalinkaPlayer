"""Who drives the output, as the replay state records it."""

import pytest
from pydantic import ValidationError

from kalinka_plugin_sdk import InputPluginContext
from kalinka_plugin_sdk.datamodel import (
    PlaybackControl,
    PlaybackControlMode,
    PlaybackMode,
    PlaybackState,
)
from kalinka_plugin_sdk.events import PlaybackControlChangedEvent, PlayQueueState

QOBUZ = PlaybackControl.exclusive("qobuz", "Qobuz Connect")


def _state() -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(),
        track_list=[],
        playback_mode=PlaybackMode(shuffle=False, repeat_single=False, repeat_all=False),
    )


def test_the_queue_drives_the_output_until_told_otherwise():
    control = _state().playback_control

    assert control.mode is PlaybackControlMode.QUEUE
    assert not control.is_exclusive


def test_the_control_is_folded_into_the_state():
    held = _state().apply(PlaybackControlChangedEvent(control=QOBUZ, seq=1))
    assert held.playback_control == QOBUZ
    assert held.playback_control.is_exclusive

    released = held.apply(PlaybackControlChangedEvent(control=PlaybackControl.queue(), seq=2))
    assert released.playback_control.mode is PlaybackControlMode.QUEUE


def test_a_stale_control_event_is_ignored():
    held = _state().apply(PlaybackControlChangedEvent(control=QOBUZ, seq=5))

    stale = PlaybackControlChangedEvent(control=PlaybackControl.queue(), seq=4)
    assert held.apply(stale) is held


def test_the_wire_names_the_mode_and_the_plugin():
    event = PlaybackControlChangedEvent(control=QOBUZ, seq=1)

    assert event.model_dump(mode="json")["control"] == {
        "mode": "exclusive",
        "plugin_id": "qobuz",
        "title": "Qobuz Connect",
    }


def test_exclusive_control_must_name_the_plugin():
    with pytest.raises(ValidationError):
        PlaybackControl(mode=PlaybackControlMode.EXCLUSIVE, plugin_id="qobuz")


def test_the_queues_control_names_no_plugin():
    with pytest.raises(ValidationError):
        PlaybackControl(plugin_id="qobuz", title="Qobuz Connect")


def test_a_context_without_direct_playback_still_builds():
    """What an older server hands a plugin: the field is simply absent."""
    context = InputPluginContext(
        logger=None,
        plugin_id="qobuz",
        sdk_version="3.3.0",
        config=None,
        listener=None,
        playqueue=None,
    )
    assert context.direct_playback is None
