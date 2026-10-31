"""An album's year survives serialization, including older saved metadata."""

import pytest

from kalinka_plugin_sdk.datamodel import Album, EntityId, EntityType


@pytest.mark.parametrize("fields,expected", [({}, None), ({"year": 1979}, 1979)])
def test_album_year_survives_json_round_trip(fields, expected):
    album = Album(
        id=EntityId(id="album", type=EntityType.ALBUM, source="test"),
        title="Album",
        **fields,
    )

    payload = album.model_dump(mode="json")
    assert payload["year"] == expected
    assert Album.model_validate_json(album.model_dump_json()).year == expected
    del payload["year"]
    assert Album.model_validate(payload).year is None
