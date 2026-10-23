"""Track positions survive serialization, including older saved metadata."""

import pytest

from kalinka_plugin_sdk.datamodel import Album, EntityId, EntityType, Track


@pytest.mark.parametrize(
    "numbers,expected",
    [
        ({}, (None, None)),
        ({"track_number": None, "disc_number": None}, (None, None)),
        ({"track_number": 7}, (7, None)),
        ({"disc_number": 2}, (None, 2)),
        ({"track_number": 7, "disc_number": 2}, (7, 2)),
    ],
)
def test_track_positions_survive_json_round_trip(numbers, expected):
    track = Track(
        id=EntityId(id="song", type=EntityType.TRACK, source="test"),
        title="Song",
        duration=180,
        album=Album(
            id=EntityId(id="album", type=EntityType.ALBUM, source="test"),
            title="Album",
        ),
        **numbers,
    )

    payload = track.model_dump(mode="json")
    assert (payload["track_number"], payload["disc_number"]) == expected
    restored = Track.model_validate_json(track.model_dump_json())
    assert (restored.track_number, restored.disc_number) == expected
