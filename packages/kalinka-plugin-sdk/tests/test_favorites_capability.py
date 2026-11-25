"""Which modules say they keep favourites."""

from kalinka_plugin_sdk import InputModule, ModuleCapability


class _Plain(InputModule):
    def module_name(self) -> str:
        return "plain"


def test_a_module_keeps_no_favourites_unless_it_says_so():
    assert ModuleCapability.FAVORITES not in _Plain().capabilities()


def test_clients_read_the_capability_as_favorites():
    assert ModuleCapability.FAVORITES.value == "favorites"
