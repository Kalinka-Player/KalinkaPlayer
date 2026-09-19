"""Judging a config change before it is kept.

A write used to be a setattr: the first thing to notice a port that was not
a number was whatever tried to bind it. Two layers answer for that now — the
declared type, and the plugin that owns the field — and the tests below hold
both to the same rule. An error refuses the whole batch; a warning is
reported and applied; a broken validator gets out of the user's way.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from pydantic import BaseModel, Field

from kalinka_plugin_sdk import ConfigIssue, IssueSeverity, ModuleConfig

from kalinka_server.config_validation import (
    ConfigKeyError,
    ConfigTargets,
    ConfigWriteError,
    DynamicFieldError,
    StaleSchemaError,
    apply_change,
    blocking,
    changes_from_payload,
    validate_changes,
)


class _Nested(BaseModel):
    volume: int = 50


class _PluginConfig(ModuleConfig):
    folders: list[str] = Field(default_factory=list)
    nested: _Nested = Field(default_factory=_Nested)


class _ServerConfig(BaseModel):
    port: int = Field(default=8000, ge=1, le=65535)
    nested: _Nested = Field(default_factory=_Nested)


class _Instance:
    """A plugin that answers whatever it was told to, and remembers what it
    was asked."""

    def __init__(self, issues=None, raises: Optional[Exception] = None):
        self._issues = issues or []
        self._raises = raises
        self.seen: list[tuple[Any, frozenset[str]]] = []

    async def validate_config(self, candidate, changed):
        self.seen.append((candidate, changed))
        if self._raises is not None:
            raise self._raises
        return list(self._issues)


@dataclass
class _Context:
    config: BaseModel


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any


def _targets(plugin_instance=None, plugin_config=None, server_config=None):
    config = plugin_config or _PluginConfig()
    return ConfigTargets(
        base_config=server_config or _ServerConfig(),
        input_modules={
            "localfiles": _Prepared(
                plugin_context=_Context(config=config),
                plugin_instance=plugin_instance or _Instance(),
            )
        },
        devices={},
    )


def _run(changes, targets):
    return asyncio.run(validate_changes(changes, targets))


class TestWhatTheBodyMustBe:
    def test_a_body_that_is_not_an_object_is_refused(self):
        with pytest.raises(ConfigWriteError):
            changes_from_payload(["port"], "v1", ())

    def test_changes_that_are_not_an_object_are_refused(self):
        with pytest.raises(ConfigWriteError):
            changes_from_payload({"changes": []}, "v1", ())

    def test_a_schema_the_server_no_longer_serves_is_refused(self):
        with pytest.raises(StaleSchemaError) as caught:
            changes_from_payload({"schema_version": "old", "changes": {}}, "v1", ())
        assert caught.value.status_code == 409

    def test_a_body_without_a_version_is_taken_at_its_word(self):
        """The version is the client's to send; a caller that omits it is
        not claiming to have read a stale one."""
        assert changes_from_payload({"changes": {"a": 1}}, "v1", ()) == {"a": 1}

    def test_a_plugin_resolved_value_cannot_be_written(self):
        with pytest.raises(DynamicFieldError) as caught:
            changes_from_payload(
                {"changes": {"input_modules.localfiles.storage.status_view": "x"}},
                "v1",
                {"input_modules.localfiles.storage.status_view"},
            )
        assert caught.value.status_code == 400


class TestWhichFieldAPathNames:
    def test_the_three_roots_resolve(self):
        targets = _targets()
        assert targets.resolve("base_config.port").attrs == ["port"]
        assert targets.resolve("input_modules.localfiles.folders").plugin is not None

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "port",
            "base_config.",
            "input_modules.localfiles",
            "input_modules.absent.folders",
            "elsewhere.localfiles.folders",
        ],
    )
    def test_a_path_that_names_nothing_writable_is_refused(self, key):
        with pytest.raises(ConfigKeyError):
            _targets().resolve(key)

    def test_the_module_name_is_not_the_users_to_change(self):
        with pytest.raises(ConfigKeyError, match="name"):
            _targets().resolve("input_modules.localfiles.name")


class TestTheTypeAValueMustBe:
    def test_a_value_of_the_wrong_type_is_an_error_against_its_own_path(self):
        issues = _run({"base_config.port": "not a port"}, _targets())
        assert [(i.path, i.severity) for i in issues] == [
            ("base_config.port", IssueSeverity.ERROR)
        ]
        assert "integer" in issues[0].message

    def test_a_value_json_spelled_as_text_is_accepted_as_its_type(self):
        """Every value arrives decoded from JSON, where a port typed into a
        text field is a string."""
        assert _run({"base_config.port": "9001"}, _targets()) == []

    def test_a_nested_path_is_checked_where_it_lands(self):
        issues = _run({"base_config.nested.volume": "loud"}, _targets())
        assert [i.path for i in issues] == ["base_config.nested.volume"]

    def test_nothing_is_written_to_the_live_configuration(self):
        server_config = _ServerConfig()
        _run({"base_config.port": 9001}, _targets(server_config=server_config))
        assert server_config.port == 8000

    def test_a_path_that_names_no_field_is_the_callers_mistake(self):
        with pytest.raises(ConfigKeyError):
            _run({"base_config.nonexistent": 1}, _targets())

    def test_a_value_outside_the_bounds_its_field_declares_is_an_error(self):
        """The bounds are published to the client as constraints; if only
        the client enforced them, a scripted save would sail past."""
        issues = _run({"base_config.port": 99999}, _targets())
        assert [(i.path, i.severity) for i in issues] == [
            ("base_config.port", IssueSeverity.ERROR)
        ]

    def test_an_out_of_bounds_value_does_not_land_on_the_copy_either(self):
        server_config = _ServerConfig()
        targets = _targets(server_config=server_config)
        _run({"base_config.port": 99999}, targets)
        assert server_config.port == 8000


class TestWhatOnlyThePluginKnows:
    def test_its_issues_come_back_under_the_full_path(self):
        instance = _Instance(
            [ConfigIssue(path="folders", index=1, message="name the share")]
        )
        issues = _run(
            {"input_modules.localfiles.folders": ["/music", "smb://nas"]},
            _targets(instance),
        )
        assert [(i.path, i.index, i.message) for i in issues] == [
            ("input_modules.localfiles.folders", 1, "name the share")
        ]

    def test_an_issue_about_the_module_itself_lands_on_the_module(self):
        """A path ending in a dot would match no field, and the message
        would be collected and never shown."""
        instance = _Instance([ConfigIssue(path="", message="not configured")])
        issues = _run(
            {"input_modules.localfiles.folders": []}, _targets(instance)
        )
        assert [i.path for i in issues] == ["input_modules.localfiles"]

    def test_it_is_asked_about_a_copy_carrying_every_change_in_the_batch(self):
        instance = _Instance()
        live = _PluginConfig()
        _run(
            {
                "input_modules.localfiles.folders": ["/music"],
                "input_modules.localfiles.nested.volume": 11,
            },
            _targets(instance, plugin_config=live),
        )
        candidate, changed = instance.seen[0]
        assert candidate is not live
        assert candidate.folders == ["/music"] and candidate.nested.volume == 11
        assert changed == frozenset({"folders", "nested.volume"})
        assert live.folders == [] and live.nested.volume == 50

    def test_it_is_asked_once_however_many_of_its_fields_changed(self):
        instance = _Instance()
        _run(
            {
                "input_modules.localfiles.folders": ["/music"],
                "input_modules.localfiles.enabled": False,
            },
            _targets(instance),
        )
        assert len(instance.seen) == 1

    def test_it_is_not_asked_about_a_change_that_never_reached_it(self):
        """A value refused by its type is not applied to the candidate, so
        asking about the rest would be asking about a configuration the user
        never described."""
        instance = _Instance()
        issues = _run({"input_modules.localfiles.nested.volume": "loud"}, _targets(instance))
        assert instance.seen == []
        assert len(issues) == 1

    def test_a_validator_that_raises_does_not_lock_the_settings_page(self):
        instance = _Instance(raises=RuntimeError("the network went away"))
        assert _run({"input_modules.localfiles.folders": []}, _targets(instance)) == []

    def test_an_answer_that_is_not_an_issue_does_not_lock_it_either(self):
        """Reading what came back is as much the plugin's fault as raising
        on the way there, and both routes go through here."""
        instance = _Instance([{"path": "folders", "message": "a plain dict"}])
        assert _run({"input_modules.localfiles.folders": []}, _targets(instance)) == []

    def test_the_server_s_own_settings_have_no_plugin_to_ask(self):
        instance = _Instance([ConfigIssue(path="x", message="never asked")])
        assert _run({"base_config.port": 9001}, _targets(instance)) == []


class TestWhatRefusesTheSave:
    def test_only_errors_do(self):
        issues = [
            ConfigIssue(path="a", message="off", severity=IssueSeverity.WARNING),
            ConfigIssue(path="b", message="wrong"),
        ]
        assert [i.path for i in blocking(issues)] == ["b"]

    def test_a_folder_that_is_merely_unreachable_does_not(self):
        instance = _Instance(
            [
                ConfigIssue(
                    path="folders",
                    index=0,
                    message="it did not answer",
                    severity=IssueSeverity.WARNING,
                )
            ]
        )
        issues = _run(
            {"input_modules.localfiles.folders": ["smb://nas/music"]},
            _targets(instance),
        )
        assert issues and not blocking(issues)


class TestWritingWhatPassed:
    def test_the_value_stored_is_the_declared_type_not_what_was_sent(self):
        config = _ServerConfig()
        assert apply_change(config, ["port"], "9001") is None
        assert config.port == 9001

    def test_a_refused_value_is_reported_rather_than_written(self):
        config = _ServerConfig()
        assert apply_change(config, ["port"], "nope") is not None
        assert config.port == 8000

    def test_a_path_that_names_no_field_is_not_a_value_problem(self):
        with pytest.raises(ConfigKeyError):
            apply_change(_ServerConfig(), ["nonexistent"], 1)
