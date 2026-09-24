"""A module's configuration brings its dependent fields in line on
construction, told which fields it was given."""

from pydantic import Field

from kalinka_plugin_sdk.module_config import ModuleConfig


class _Counted(ModuleConfig):
    words: list[str] = Field(default_factory=list)
    count: int = 0
    given: list[str] = Field(default_factory=list)

    def reconcile(self, written: frozenset[str]) -> None:
        self.count = len(self.words)
        self.given = sorted(written)


def test_the_fields_given_are_reconciled():
    counted = _Counted(words=["a", "b"])
    assert counted.count == 2
    assert counted.given == ["words"]


def test_defaults_are_reconciled_too():
    assert _Counted().given == []


def test_a_module_with_nothing_to_reconcile_keeps_what_it_was_given():
    assert ModuleConfig(enabled=False).enabled is False
