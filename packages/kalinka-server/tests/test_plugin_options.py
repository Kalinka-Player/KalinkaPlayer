"""Suggestions a plugin offers for its own fields.

A field says it wants them by a tag on itself, and the tag is the whole of
the declaration: the server reads it off the built schema and binds the
field to the plugin that owns it. What comes back is a suggestion and not a
choice, so nothing here may fail the settings page — a plugin that answers
badly, or not at all, costs its own field's list and nothing else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from kalinka_plugin_sdk import ConfigOption, ModuleConfig

from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_enum_options, build_presentation
from kalinka_server.options_registry import OptionsRegistry, register_plugin_options
from kalinka_server.presentation_schema import Widget


class _Nested(BaseModel):
    host: str = Field(default="", json_schema_extra={"dynamic_options": True})


class _PluginConfig(ModuleConfig):
    folders: list[str] = Field(
        default_factory=list,
        json_schema_extra={"widget": "folder_list", "dynamic_options": True},
    )
    plain: str = ""
    loud: bool = Field(default=False, json_schema_extra={"dynamic_options": True})
    nested: _Nested = Field(default_factory=_Nested)


class _Instance:
    def __init__(self, answers=None, raises: Exception | None = None):
        self._answers = answers or {}
        self._raises = raises
        self.asked: list[str] = []

    async def resolve_options(self, path: str):
        self.asked.append(path)
        if self._raises is not None:
            raise self._raises
        if path not in self._answers:
            raise KeyError(path)
        return self._answers[path]


@dataclass
class _Context:
    config: BaseModel


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any


def _config():
    """Named, because a module's dotted path is built from the name its
    config carries rather than from the key it is loaded under."""
    return _PluginConfig(name="localfiles")


def _schema(config=None):
    return build_presentation(
        base_config=KalinkaConfig(),
        input_modules={"localfiles": config or _config()},
        devices={},
    )


def _registry(instance, config=None):
    config = config or _config()
    registry = OptionsRegistry()
    register_plugin_options(
        registry,
        _schema(config),
        {
            "localfiles": _Prepared(
                plugin_context=_Context(config=config),
                plugin_instance=instance,
            )
        },
        {},
    )
    return registry


def _field(schema, path):
    return next(f for f in schema.expert_fields if f.path == path)


class TestSayingAFieldWantsSuggestions:
    def test_the_tag_reaches_the_schema(self):
        field = _field(_schema(), "input_modules.localfiles.folders")
        assert field.dynamic_options is True

    def test_an_untagged_field_does_not_claim_them(self):
        assert _field(_schema(), "input_modules.localfiles.plain").dynamic_options is False

    def test_a_tagged_field_inside_a_section_is_found_too(self):
        field = _field(_schema(), "input_modules.localfiles.nested.host")
        assert field.dynamic_options is True

    def test_a_widget_with_nowhere_to_show_them_drops_the_tag(self, caplog):
        with caplog.at_level("WARNING"):
            field = _field(_schema(), "input_modules.localfiles.loud")
        assert field.widget == Widget.TOGGLE
        assert field.dynamic_options is False
        assert "cannot show suggestions" in caplog.text


class TestAskingThePluginThatOwnsIt:
    def test_only_tagged_fields_are_bound(self):
        registry = _registry(_Instance())
        assert sorted(registry.paths()) == [
            "input_modules.localfiles.folders",
            "input_modules.localfiles.nested.host",
        ]

    def test_the_plugin_is_asked_for_the_path_as_it_spells_it(self):
        instance = _Instance({"nested.host": []})
        registry = _registry(instance)
        asyncio.run(registry.resolve("input_modules.localfiles.nested.host"))
        assert instance.asked == ["nested.host"]

    def test_what_it_answers_reaches_the_values_envelope(self):
        instance = _Instance(
            {
                "folders": [
                    ConfigOption(
                        value="smb://nas/", label="NAS", description="found over mDNS"
                    )
                ]
            }
        )
        options = asyncio.run(build_enum_options(_registry(instance)))
        offered = options["input_modules.localfiles.folders"]
        assert [(o.value, o.label, o.description) for o in offered] == [
            ("smb://nas/", "NAS", "found over mDNS")
        ]

    def test_a_field_no_loaded_plugin_owns_is_left_unbound(self, caplog):
        registry = OptionsRegistry()
        with caplog.at_level("WARNING"):
            register_plugin_options(registry, _schema(), {}, {})
        assert registry.paths() == []
        assert "no loaded plugin owns it" in caplog.text


class TestWhenThePluginCannotAnswer:
    def test_a_path_it_does_not_recognise_costs_only_its_own_list(self, caplog):
        instance = _Instance({"folders": []})
        registry = _registry(instance)
        with caplog.at_level("WARNING"):
            options = asyncio.run(build_enum_options(registry))
        assert options["input_modules.localfiles.nested.host"] == []
        assert options["input_modules.localfiles.folders"] == []
        assert "does not resolve it" in caplog.text

    def test_a_resolver_that_raises_is_left_out_rather_than_breaking_the_read(self):
        registry = _registry(_Instance(raises=RuntimeError("the network went away")))
        assert asyncio.run(build_enum_options(registry)) == {}

    def test_a_lookup_that_failed_inside_the_plugin_is_not_read_as_a_refusal(
        self, caplog
    ):
        """Only ``KeyError(path)`` is the plugin saying it does not own the
        field; blaming the declaration for any other one hides the fault."""
        registry = _registry(_Instance(raises=KeyError("some other mapping")))
        with caplog.at_level("WARNING"):
            assert asyncio.run(build_enum_options(registry)) == {}
        assert "does not resolve it" not in caplog.text
