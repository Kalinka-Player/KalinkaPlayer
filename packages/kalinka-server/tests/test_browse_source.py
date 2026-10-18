"""The registry of browsable sources: plugins as they stand right now, plus
the server's own."""

from kalinka_server.browse_source import BrowseSourceRegistry, RegisteredSource


class _Source:
    def __init__(self, name):
        self._name = name

    def module_name(self):
        return self._name


def _entry(name, *, builtin=False):
    return RegisteredSource(
        name=name, title=name.title(), source=_Source(name), builtin=builtin
    )


def test_sources_are_listed_by_name_whoever_provides_them():
    registry = BrowseSourceRegistry(
        lambda: [_entry("qobuz"), _entry("localfiles")],
        [_entry("collections", builtin=True)],
    )

    assert [entry.name for entry in registry.entries()] == [
        "collections",
        "localfiles",
        "qobuz",
    ]


def test_a_plugin_disabled_after_startup_stops_being_reachable():
    enabled = {"qobuz"}
    registry = BrowseSourceRegistry(
        lambda: [_entry(name) for name in enabled],
        [_entry("collections", builtin=True)],
    )
    assert registry.get("qobuz") is not None

    enabled.clear()

    assert registry.get("qobuz") is None
    assert registry.get("collections") is not None


def test_an_unknown_name_resolves_to_nothing():
    registry = BrowseSourceRegistry(lambda: [], [_entry("collections", builtin=True)])

    assert registry.get("nope") is None


def test_a_builtin_says_so():
    registry = BrowseSourceRegistry(
        lambda: [_entry("qobuz")], [_entry("collections", builtin=True)]
    )

    builtin = {entry.name: entry.builtin for entry in registry.entries()}

    assert builtin == {"collections": True, "qobuz": False}


def _root_names(listing):
    return [item.id.source for item in listing.items]


def test_the_root_pages_through_every_source():
    registry = BrowseSourceRegistry(
        lambda: [_entry("qobuz"), _entry("localfiles"), _entry("jamendo")],
        [_entry("collections", builtin=True)],
    )

    first = registry.root_listing(offset=0, limit=3)
    rest = registry.root_listing(offset=3, limit=3)

    assert first.total == rest.total == 4
    assert _root_names(first) == ["collections", "jamendo", "localfiles"]
    assert _root_names(rest) == ["qobuz"]
    assert registry.root_listing(offset=4, limit=3).items == []


def test_a_root_entry_is_the_sources_root_catalog():
    registry = BrowseSourceRegistry(lambda: [_entry("jamendo")])

    (item,) = registry.root_listing(offset=0, limit=10).items

    assert item.id.to_string == "kalinka:jamendo:catalog:root"
    assert item.can_browse and not item.can_add
    assert item.catalog.id == item.id
    assert item.catalog.title == item.name == "Jamendo"
