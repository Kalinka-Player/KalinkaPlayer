"""A setting that is a list of records, credentials inside.

Contract:

* ``GET /server/config`` carries the list with every credential left out; a
  credential that holds something is named in ``secrets_set`` by the entry's
  id, ``<path>.<id>.<field>``. A list of models that are not records is named
  by position, and leaks nothing either.
* How the settings page shows the list is ``test_config_collections``'s.
* A client writes the whole list back. An entry that leaves its credential
  out keeps the saved one while it keeps its id, its type and its credential
  scope; an empty credential clears it and a new one replaces it. The dry
  run and the save keep the same one, and the overrides file keeps it too.
* A saved credential an entry leaves out but cannot keep, because the entry
  now names another server or user or is of another kind, is an error at
  that credential's path, in the dry run as in the save.
* Two entries with one id are refused; an entry without one is given one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

import pytest
from pydantic import BaseModel, Field

from kalinka_plugin_sdk import ConfigRecord, IssueSeverity, Records
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import PluginBase, PluginType
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix
from kalinka_server.config_schema_processor import build_values
from kalinka_server.config_secrets import is_private_path, secret_values
from kalinka_server.config_validation import (
    ConfigTargets,
    apply_change,
    commit_change,
    stored_value,
    validate_changes,
)
from kalinka_server.player_setup import PreparedModuleCollection

SECRET = "hunter2-do-not-log"
_PASSWORD = {"widget": "password"}


class _Account(BaseModel):
    mode: Literal["account"] = "account"
    user: str = ""
    password: str = Field(default="", json_schema_extra=_PASSWORD)


class _Guest(BaseModel):
    mode: Literal["guest"] = "guest"


class _Server(ConfigRecord):
    kind: Literal["server"] = "server"
    host: str = ""
    login: Union[_Guest, _Account] = Field(default_factory=_Guest, discriminator="mode")

    def credential_scope(self):
        return self.host


class _Mirror(ConfigRecord):
    kind: Literal["mirror"] = "mirror"
    host: str = ""
    login: Union[_Guest, _Account] = Field(default_factory=_Guest, discriminator="mode")

    def credential_scope(self):
        return self.host


class _Folder(ConfigRecord):
    kind: Literal["folder"] = "folder"
    path: str = ""
    password: str = Field(default="", json_schema_extra=_PASSWORD)


_Entry = Annotated[Union[_Server, _Mirror, _Folder], Field(discriminator="kind")]


class _Config(ModuleConfig):
    name: str = Field(default="mod", frozen=True, exclude=True)
    entries: Records[_Entry] = Field(default_factory=list)
    accounts: list[_Account] = Field(default_factory=list)
    port: int = 8000


class _Plugin(PluginBase):
    PLUGIN_ID = "mod"
    REQUIRES_SDK = "1.0"
    PLUGIN_TYPE = PluginType.INPUT_MODULE
    CONFIG_MODEL = _Config

    async def setup(self, context):
        return None


class _Judge:
    """A plugin that remembers what it was asked to judge."""

    def __init__(self):
        self.candidates = []

    async def validate_config(self, candidate, changed):
        self.candidates.append(candidate)
        return []


@dataclass
class _Context:
    config: BaseModel


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any = None


def _server(id="nas", host="nas", **login):
    return {
        "id": id,
        "kind": "server",
        "host": host,
        "login": {"mode": "account", "user": "media", **login},
    }


def _saved() -> _Config:
    return _Config(entries=[_server(password=SECRET)])


def _password(config: _Config, index=0) -> str:
    return getattr(config.entries[index].login, "password", None)


def _write(config: _Config, entries) -> str | None:
    return apply_change(config, ["entries"], entries).refused


def _targets(config, plugin=None) -> ConfigTargets:
    return ConfigTargets(
        base_config=KalinkaConfig(),
        input_modules={"mod": _Prepared(_Context(config), plugin)},
        devices={},
    )


def _values(**modules):
    return asyncio.run(build_values(KalinkaConfig(), modules, {}))


class TestWhatAClientIsSent:
    def test_a_credential_is_left_out_and_named_by_its_entry(self):
        known = _values(mod=_saved())

        assert SECRET not in json.dumps(known.values)
        assert known.values["input_modules.mod.entries"] == [
            {
                "id": "nas",
                "kind": "server",
                "host": "nas",
                "login": {"mode": "account", "user": "media"},
            }
        ]
        assert known.secrets_set == {"input_modules.mod.entries.nas.login.password"}

    def test_an_empty_credential_is_not_named(self):
        known = _values(mod=_Config(entries=[_server()]))
        assert known.secrets_set == frozenset()

    def test_models_that_are_not_records_are_named_by_position(self):
        config = _Config(accounts=[_Account(), _Account(password=SECRET)])
        known = _values(mod=config)

        assert SECRET not in json.dumps(known.values)
        assert known.secrets_set == {"input_modules.mod.accounts.1.password"}


class TestWritingTheListBack:
    def test_a_credential_left_out_is_kept(self):
        config = _saved()
        assert _write(config, [_server()]) is None
        assert _password(config) == SECRET

    def test_an_empty_credential_clears_it(self):
        config = _saved()
        _write(config, [_server(password="")])
        assert _password(config) == ""

    def test_a_new_credential_replaces_it(self):
        config = _saved()
        _write(config, [_server(password="new")])
        assert _password(config) == "new"

    def test_it_follows_its_entry_wherever_it_moves(self):
        config = _Config(
            entries=[
                _server(id="a", host="a", password="for-a"),
                _server(id="b", host="b"),
            ]
        )
        _write(config, [_server(id="b", host="b"), _server(id="a", host="a")])
        assert [_password(config, i) for i in (0, 1)] == ["", "for-a"]

    def test_an_entry_pointed_at_another_server_does_not_take_it_along(self):
        """Otherwise a saved password would be sent to whatever server the
        entry is edited to name."""
        config = _saved()
        _write(config, [_server(host="elsewhere")])
        assert _password(config) == ""

    def test_a_new_entry_does_not_inherit_one(self):
        config = _saved()
        _write(config, [_server(id="other")])
        assert _password(config) == ""

    def test_an_entry_of_another_type_does_not_inherit_one(self):
        config = _Config(entries=[{"id": "x", "kind": "folder", "password": SECRET}])
        _write(config, [{"id": "x", "kind": "server", "login": {"mode": "account"}}])
        assert _password(config) == ""

    def test_a_login_switched_to_guest_holds_none(self):
        config = _saved()
        _write(config, [{**_server(), "login": {"mode": "guest"}}])
        assert SECRET not in list(secret_values(config))

    def test_two_entries_with_one_id_are_refused(self):
        config = _saved()
        reason = _write(config, [_server(), _server(host="other")])
        assert "nas" in reason
        assert [e.host for e in config.entries] == ["nas"]

    def test_an_entry_without_an_id_is_given_one(self):
        config = _Config()
        _write(config, [{"kind": "folder", "path": "/music"}])
        assert config.entries[0].id

    def test_the_dry_run_judges_with_the_kept_credential(self):
        """A share is only reachable with the password the save will keep."""
        judge = _Judge()
        issues = asyncio.run(
            validate_changes(
                {"input_modules.mod.entries": [_server()]},
                _targets(_saved(), judge),
            )
        )
        assert issues == []
        assert _password(judge.candidates[0]) == SECRET

    def test_the_overrides_file_keeps_the_credential_too(self):
        """What was sent holds no password; a restart reads the file."""
        config = _saved()
        target = _targets(config).resolve("input_modules.mod.entries")
        commit_change(target, "input_modules.mod.entries", [_server()])

        restarted = _Config()
        apply_overrides_with_prefix(
            restarted,
            {"input_modules.mod.entries": stored_value(target)},
            "input_modules.mod.",
        )
        assert _password(restarted) == SECRET
        assert restarted.entries[0].id == "nas"


class TestACredentialThatCannotBeKept:
    """The client was never sent it, so it cannot tell it is gone."""

    _PATH = "input_modules.mod.entries.nas.login.password"

    def _issues(self, entries, config=None):
        return asyncio.run(
            validate_changes(
                {"input_modules.mod.entries": entries},
                _targets(config or _saved(), _Judge()),
            )
        )

    def test_it_is_refused_where_the_credential_is(self):
        [issue] = self._issues([_server(host="elsewhere")])
        assert issue.path == self._PATH
        assert issue.severity == IssueSeverity.ERROR
        assert issue.message == (
            "enter the password again: the saved one was for another server or user"
        )

    def test_an_entry_of_another_kind_says_so(self):
        [issue] = self._issues([{**_server(), "kind": "mirror"}])
        assert issue.path == self._PATH
        assert "another kind of entry" in issue.message

    @pytest.mark.parametrize("password", ["new", ""])
    def test_one_sent_again_is_what_was_asked_for(self, password):
        assert self._issues([_server(host="elsewhere", password=password)]) == []

    def test_one_that_is_kept_is_not_reported(self):
        assert self._issues([_server()]) == []

    def test_an_entry_switched_to_a_guest_needs_none(self):
        guest = {**_server(host="elsewhere"), "login": {"mode": "guest"}}
        assert self._issues([guest]) == []

    def test_a_new_entry_had_none_to_lose(self):
        assert self._issues([_server(id="other", host="elsewhere")]) == []

    def test_an_entry_that_had_none_saved_loses_none(self):
        config = _Config(entries=[_server()])
        assert self._issues([_server(host="elsewhere")], config) == []

    def test_the_save_finds_the_same_one(self):
        config = _saved()
        applied = apply_change(config, ["entries"], [_server(host="elsewhere")])
        issues = applied.issues("input_modules.mod.entries")
        assert [issue.path for issue in issues] == [self._PATH]


class TestLogs:
    def test_saving_the_list_names_it_and_nothing_more(self, caplog):
        caplog.set_level(logging.DEBUG)
        config = _saved()
        target = _targets(config).resolve("input_modules.mod.entries")
        commit_change(target, "input_modules.mod.entries", [_server(password="new")])

        assert "new" not in caplog.text.replace("input_modules", "")
        assert "Updated config field input_modules.mod.entries" in caplog.text

    def test_a_list_holding_a_credential_is_private(self):
        assert is_private_path(_Config, ["entries"])
        assert not is_private_path(_Config, ["port"])

    def test_an_entry_s_credential_is_one_the_log_export_looks_for(self):
        assert SECRET in list(secret_values(_saved()))


class TestReconcilingAfterSetup:
    def test_a_saved_list_is_left_as_it_is(self):
        """Compared as the file stores it; a mismatch would rewrite it on
        every start."""
        config = _saved()
        target = _targets(config).resolve("input_modules.mod.entries")
        overrides = {"input_modules.mod.entries": stored_value(target)}
        before = json.dumps(overrides, sort_keys=True)

        changed = PreparedModuleCollection()._reconcile_consumed_overrides(
            "mod", _Plugin, config, overrides
        )

        assert changed == 0
        assert json.dumps(overrides, sort_keys=True) == before


def test_stored_records_serialise_for_the_overrides_file():
    config = _saved()
    value = stored_value(_targets(config).resolve("input_modules.mod.entries"))
    assert json.loads(json.dumps(value)) == value


@pytest.mark.parametrize("record_id", ["has.dot", "", "a" * 65, "sp ace"])
def test_an_id_that_cannot_name_an_entry_in_a_path_is_refused(record_id):
    reason = _write(_Config(), [{"id": record_id, "kind": "folder"}])
    assert reason is not None
