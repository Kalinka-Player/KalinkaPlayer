"""Which modules say they keep favourites, and what the rest answer."""

import pytest

from kalinka_plugin_sdk import InputModule, ModuleCapability
from kalinka_plugin_sdk.datamodel import FavoriteIds
from kalinka_plugin_sdk.inputmodule import SearchType


class _Plain(InputModule):
    def module_name(self) -> str:
        return "plain"


def test_a_module_keeps_no_favourites_unless_it_says_so():
    assert ModuleCapability.FAVORITES not in _Plain().capabilities()


def test_clients_read_the_capability_as_favorites():
    assert ModuleCapability.FAVORITES.value == "favorites"


async def test_a_module_that_keeps_none_reads_back_none():
    page = await _Plain().list_favorite(SearchType.track, "", offset=10, limit=5)
    assert (page.items, page.total, page.offset, page.limit) == ([], 0, 10, 5)
    assert await _Plain().get_favorite_ids() == FavoriteIds()


@pytest.mark.parametrize("change", ["add_to_favorite", "remove_from_favorite"])
async def test_a_module_that_keeps_none_has_nothing_to_mark(change):
    with pytest.raises(NotImplementedError, match="plain keeps no favourites"):
        await getattr(_Plain(), change)("kalinka:plain:track:1")
