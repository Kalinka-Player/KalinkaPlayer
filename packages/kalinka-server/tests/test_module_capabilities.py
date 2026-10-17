"""What a source says it can answer.

Derived from the module rather than declared by it, so a plugin gains a
capability by writing the method — and a source that is not a module at all
has none.
"""

from kalinka_plugin_sdk.inputmodule import InputModule

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

    def ai_search_available(self):
        return self._on


class _BrokenSwitch(_Semantic):
    def ai_search_available(self):
        raise RuntimeError("boom")


class _Collections:
    """Browses and matches names, streams nothing."""

    def module_name(self):
        return "collections"


def test_a_module_that_writes_the_call_has_the_capability():
    assert capabilities_of(_Semantic()) == ["ai_search"]


def test_leaving_the_sdk_default_alone_is_asking_not_to_be_asked():
    assert capabilities_of(_Plain()) == []


def test_the_per_call_budget_does_not_hide_what_it_wraps():
    """The proxy binds its delegates onto the instance, so the wrapped class
    is the only thing that still knows."""
    assert capabilities_of(TimeLimitedInputModule(_Semantic(), "semantic")) == [
        "ai_search"
    ]
    assert capabilities_of(TimeLimitedInputModule(_Plain(), "plain")) == []


def test_a_call_switched_off_by_its_setting_is_not_advertised():
    assert capabilities_of(_Switchable(on=True)) == ["ai_search"]
    assert capabilities_of(_Switchable(on=False)) == []
    assert capabilities_of(TimeLimitedInputModule(_Switchable(on=False), "s")) == []


def test_a_switch_that_raises_counts_as_off():
    assert capabilities_of(_BrokenSwitch()) == []


def test_a_source_that_is_not_a_module_has_none():
    assert capabilities_of(_Collections()) == []
    assert capabilities_of(None) == []
