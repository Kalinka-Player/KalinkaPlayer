"""The modules as the replay state records them."""

from kalinka_plugin_sdk import (
    ModuleDescriptor,
    ModuleHealthState,
    ModulesChangedEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState

QOBUZ = ModuleDescriptor(
    name="qobuz",
    title="Qobuz",
    state=ModuleHealthState.ERROR,
    error_message="Not paired yet.",
)
AMP = ModuleDescriptor(
    name="musiccast", title="MusicCast", state=ModuleHealthState.READY
)


def _state() -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(),
        track_list=[],
        playback_mode=PlaybackMode(
            shuffle=False, repeat_single=False, repeat_all=False
        ),
    )


def test_a_state_holds_no_modules_until_told():
    assert _state().modules is None


def test_the_modules_are_folded_into_the_state():
    state = _state().apply(
        ModulesChangedEvent(input_modules=[QOBUZ], devices=[AMP], seq=1)
    )

    assert state.modules.input_modules == [QOBUZ]
    assert state.modules.devices == [AMP]
    assert state.seq == 1


def test_a_stale_modules_event_is_ignored():
    state = _state().apply(
        ModulesChangedEvent(input_modules=[QOBUZ], devices=[], seq=5)
    )

    stale = ModulesChangedEvent(input_modules=[], devices=[], seq=4)
    assert state.apply(stale) is state


def test_the_wire_lists_the_modules_as_the_module_listing_does():
    wire = ModulesChangedEvent(input_modules=[QOBUZ], devices=[AMP]).model_dump(
        mode="json"
    )

    assert wire["event_type"] == PlayQueueEventType.ModulesChanged.value
    assert wire["input_modules"] == [
        {
            "name": "qobuz",
            "title": "Qobuz",
            "icon": None,
            "icons": None,
            "enabled": True,
            "state": "error",
            "error_message": "Not paired yet.",
            "missing_packages": [],
            "builtin": False,
            "capabilities": [],
        }
    ]
    assert wire["devices"][0]["state"] == "ready"
