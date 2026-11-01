"""How a library says what it makes of storage, and how it takes some in."""

import pytest
from pydantic import ValidationError

from kalinka_plugin_sdk.datamodel import Catalog, EntityId, EntityType
from kalinka_plugin_sdk.storage import LibraryAdopter, SourcesChange, StorageBacked


def test_a_change_adds_one_record_or_removes_one():
    assert SourcesChange(collection="music_sources", add={"kind": "local"}).remove is None
    assert SourcesChange(collection="music_sources", remove="s1").add is None
    with pytest.raises(ValidationError):
        SourcesChange(collection="music_sources")
    with pytest.raises(ValidationError):
        SourcesChange(collection="music_sources", add={}, remove="s1")


def test_a_library_opts_in_by_answering_all_three():
    class Library:
        async def location_status(self, location):
            return None

        async def adopt_location(self, location):
            return None

        async def release_location(self, location):
            return None

    class StatusOnly:
        async def location_status(self, location):
            return None

    assert isinstance(Library(), LibraryAdopter)
    assert not isinstance(StatusOnly(), LibraryAdopter)
    assert not isinstance(Library(), StorageBacked)


def test_a_catalog_is_neither_live_nor_on_storage_unless_it_says():
    catalog = Catalog(id=EntityId(id="x", type=EntityType.CATALOG, source="s"), title="X")

    assert (catalog.live, catalog.on_storage) == (False, False)
