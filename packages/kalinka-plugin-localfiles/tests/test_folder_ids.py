"""A folder id carries its location both ways, in an entity id's alphabet."""

import pytest

from kalinka_plugin_localfiles.folder_ids import decode_folder_id, encode_folder_id


@pytest.mark.parametrize(
    "path",
    [
        "/music",
        "/",
        "/music/Ñu/über Ü",
        "/music/Борис Гребенщиков/1985",
        "smb://nas:4450/share/Music",
        "smb://[fe80::1]/music/a b",
        "/a",
        "/ab",
        "/abc",
        "/abcd",
    ],
)
def test_a_location_round_trips_through_its_id(path):
    folder_id = encode_folder_id(path)

    assert decode_folder_id(folder_id) == path
    assert ":" not in folder_id and "/" not in folder_id and "=" not in folder_id


@pytest.mark.parametrize(
    "folder_id",
    ["", "L211c2lj+", "L211c2lj/", "L211c2lj==", "L", "L211c", "_w", "AA"],
)
def test_an_id_naming_no_location_is_refused(folder_id):
    assert decode_folder_id(folder_id) is None
