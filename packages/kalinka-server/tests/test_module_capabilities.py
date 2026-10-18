"""What a source says it can answer.

The module reports it (``InputModule.capabilities``), so a capability can
follow the module's settings; the SDK default infers AI search from the
method, as servers did before, and never infers catalogs — and a source that
is not a module at all has none.
"""

from kalinka_plugin_sdk.inputmodule import InputModule, ModuleCapability

from kalinka_server.module_capabilities import capabilities_of
from kalinka_server.module_timeout import TimeLimitedInputModule


class _Plain(InputModule):
    def module_name(self):
        return "plain"


class _Semantic(InputModule):
    def module_name(self):
        return "semantic"

    async def ai_search(self, query, offset=0, limit=50):
        return None


class _Switchable(_Semantic):
    """Writes the call behind a setting."""

    def __init__(self, on):
        self._on = on

    def capabilities(self):
        return frozenset({ModuleCapability.AI_SEARCH}) if self._on else frozenset()


class _Reporting(_Plain):
    def __init__(self, reported):
        self._reported = reported

    def capabilities(self):
        return self._reported


class _Catalogs(_Plain):
    """Has catalogs to show, and says so."""

    def capabilities(self):
        return frozenset({ModuleCapability.CATALOGS})


class _CatalogsAndSemantic(_Semantic):
    def capabilities(self):
        return frozenset({ModuleCapability.CATALOGS, ModuleCapability.AI_SEARCH})


class _Receiver(_Plain):
    """Browses to nothing; a controller hands it what it plays."""

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        return None


class _Broken(_Semantic):
    def capabilities(self):
        raise RuntimeError("boom")


class _Collections:
    """Browses and matches names, streams nothing."""

    def module_name(self):
        return "collections"


def test_the_default_infers_ai_search_from_the_method():
    assert capabilities_of(_Semantic()) == ["ai_search"]
    assert capabilities_of(_Plain()) == []


def test_a_module_reports_what_its_settings_allow():
    assert capabilities_of(_Switchable(on=True)) == ["ai_search"]
    assert capabilities_of(_Switchable(on=False)) == []


def test_catalogs_are_listed_only_where_the_module_declares_them():
    assert capabilities_of(_Catalogs()) == ["catalogs"]
    assert capabilities_of(_CatalogsAndSemantic()) == ["ai_search", "catalogs"]


def test_catalogs_are_not_inferred_from_browsing():
    assert capabilities_of(_Receiver()) == []
    assert capabilities_of(_Semantic()) == ["ai_search"]


def test_the_per_call_budget_does_not_hide_what_it_wraps():
    assert capabilities_of(TimeLimitedInputModule(_Semantic(), "semantic")) == [
        "ai_search"
    ]
    assert capabilities_of(TimeLimitedInputModule(_Switchable(False), "s")) == []
    assert capabilities_of(TimeLimitedInputModule(_Plain(), "plain")) == []
    assert capabilities_of(TimeLimitedInputModule(_Catalogs(), "c")) == ["catalogs"]
    assert capabilities_of(TimeLimitedInputModule(_Receiver(), "r")) == []


def test_only_known_capabilities_are_listed():
    assert capabilities_of(_Reporting({"ai_search", "telepathy"})) == ["ai_search"]


def test_a_report_that_raises_lists_none():
    assert capabilities_of(_Broken()) == []


def test_a_source_that_is_not_a_module_has_none():
    assert capabilities_of(_Collections()) == []


def test_a_disabled_plugin_has_none():
    assert capabilities_of(None) == []
