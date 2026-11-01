"""The volumes as the replay state records them."""

from kalinka_plugin_sdk import (
    PlayQueueEventType,
    PlayQueueState,
    VolumeDescriptor,
    VolumeKind,
    VolumeLibrary,
    VolumesChangedEvent,
)
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState

STICK = VolumeDescriptor(
    id="3A1F-9C2B",
    kind=VolumeKind.REMOVABLE,
    label="SANDISK",
    mount_point="/media/kalinka/3A1F-9C2B",
    fs_type="exfat",
    library=VolumeLibrary(module="localfiles", title="My Library", holds=False, library_empty=True),
)


def _state() -> PlayQueueState:
    return PlayQueueState(
        playback_state=PlaybackState(),
        track_list=[],
        playback_mode=PlaybackMode(shuffle=False, repeat_single=False, repeat_all=False),
    )


def test_a_state_holds_no_volumes_until_told():
    assert _state().volumes is None


def test_the_volumes_are_folded_into_the_state():
    state = _state().apply(VolumesChangedEvent(volumes=[STICK], seq=1))

    assert state.volumes == [STICK]


def test_a_stale_volumes_event_is_ignored():
    state = _state().apply(VolumesChangedEvent(volumes=[STICK], seq=5))

    assert state.apply(VolumesChangedEvent(volumes=[], seq=4)) is state


def test_the_wire_names_each_volume_as_the_listing_does():
    wire = VolumesChangedEvent(volumes=[STICK]).model_dump(mode="json")

    assert wire["event_type"] == PlayQueueEventType.VolumesChanged.value == "volumes_changed"
    [volume] = wire["volumes"]
    assert volume["kind"] == "removable" and volume["state"] == "mounted"
    assert volume["library"] == {
        "module": "localfiles",
        "title": "My Library",
        "holds": False,
        "library_empty": True,
    }
