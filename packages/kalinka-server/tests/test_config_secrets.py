"""Credentials in the configuration never leave the server in plain text.

Contract:

* A field with the password widget is a credential. ``GET /server/config``
  never carries its value — only whether it is set — and the schema never
  carries its default.
* No log line the server writes about configuration shows a credential:
  not the save, not the reconcile after plugin setup, not an override that
  fails to apply, not a default the emitter warns about.
* A credential the type refuses is refused without being quoted back.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, Field

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import PluginBase, PluginType
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix
from kalinka_server.config_schema_processor import build_presentation, build_values
from kalinka_server.config_secrets import REDACTED
from kalinka_server.config_validation import (
    ConfigTargets,
    commit_change,
    validate_changes,
)
from kalinka_server.player_setup import PreparedModuleCollection

SECRET = "hunter2-do-not-log"
_PASSWORD = {"widget": "password"}


class _Login(BaseModel):
    password: str = Field(default="", max_length=32, json_schema_extra=_PASSWORD)


class _Config(ModuleConfig):
    name: str = Field(default="mod", frozen=True, exclude=True)
    token: str = Field(default="", json_schema_extra=_PASSWORD)
    login: _Login = Field(default_factory=_Login)
    port: int = 8000


class _Plugin(PluginBase):
    PLUGIN_ID = "mod"
    REQUIRES_SDK = "1.0"
    PLUGIN_TYPE = PluginType.INPUT_MODULE
    CONFIG_MODEL = _Config

    async def setup(self, context):
        return None


@dataclass
class _Context:
    config: BaseModel


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any = None


def _targets(config: _Config) -> ConfigTargets:
    return ConfigTargets(
        base_config=KalinkaConfig(),
        input_modules={"mod": _Prepared(_Context(config))},
        devices={},
    )


def _values(**modules):
    return asyncio.run(build_values(KalinkaConfig(), modules, {}))


@pytest.fixture
def log(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def test_a_set_credential_is_named_but_its_value_is_not_sent():
    known = _values(mod=_Config(token=SECRET, login=_Login(password=SECRET)))

    assert "input_modules.mod.token" not in known.values
    assert "input_modules.mod.login.password" not in known.values
    assert SECRET not in known.values.values()
    assert known.secrets_set == {
        "input_modules.mod.token",
        "input_modules.mod.login.password",
    }


def test_an_empty_credential_is_not_reported_as_set():
    known = _values(mod=_Config())

    assert "input_modules.mod.token" not in known.values
    assert known.secrets_set == frozenset()


def test_the_rest_of_the_configuration_is_still_sent():
    assert _values(mod=_Config(token=SECRET)).values["input_modules.mod.port"] == 8000


def test_the_shipped_credentials_are_treated_as_such():
    config = LocalFilesConfig()
    config.smb.password = SECRET
    config.enricher.plugins.acoustid.api_key = SECRET

    known = _values(localfiles=config)

    assert SECRET not in known.values.values()
    assert {
        "input_modules.localfiles.smb.password",
        "input_modules.localfiles.enricher.plugins.acoustid.api_key",
    } <= known.secrets_set


def test_the_schema_does_not_carry_a_credential_s_default():
    class _WithDefault(ModuleConfig):
        name: str = Field(default="dflt", frozen=True, exclude=True)
        key: str = Field(default=SECRET, json_schema_extra=_PASSWORD)

    schema = build_presentation(KalinkaConfig(), {"dflt": _WithDefault()}, {})
    field = next(
        f for f in schema.expert_fields if f.path == "input_modules.dflt.key"
    )

    assert field.default is None
    assert SECRET not in schema.model_dump_json()


def test_saving_a_credential_logs_it_redacted(log):
    config = _Config()
    targets = _targets(config)

    for key in ("input_modules.mod.token", "input_modules.mod.login.password"):
        assert commit_change(targets.resolve(key), key, SECRET) is None

    assert config.token == SECRET
    assert config.login.password == SECRET
    assert SECRET not in log.text
    assert REDACTED in log.text


def test_saving_a_group_that_holds_a_credential_logs_it_redacted(log):
    config = _Config()
    targets = _targets(config)
    key = "input_modules.mod.login"

    assert commit_change(targets.resolve(key), key, {"password": SECRET}) is None

    assert config.login.password == SECRET
    assert SECRET not in log.text


def test_a_password_written_into_a_url_is_masked(log):
    class _Folders(ModuleConfig):
        name: str = Field(default="dirs", frozen=True, exclude=True)
        folders: list[str] = Field(default_factory=list)

    targets = ConfigTargets(
        base_config=KalinkaConfig(),
        input_modules={"dirs": _Prepared(_Context(_Folders()))},
        devices={},
    )
    key = "input_modules.dirs.folders"

    commit_change(targets.resolve(key), key, [f"smb://alice:{SECRET}@nas/music"])

    assert "smb://alice:<secret>@nas/music" in log.text
    assert SECRET not in log.text


def test_saving_anything_else_still_logs_the_value(log):
    targets = _targets(_Config())

    commit_change(targets.resolve("input_modules.mod.port"), "input_modules.mod.port", 9001)

    assert "9001" in log.text


def test_a_refused_credential_is_not_quoted_back(log):
    too_long = SECRET * 3
    issues = asyncio.run(
        validate_changes(
            {"input_modules.mod.login.password": too_long}, _targets(_Config())
        )
    )

    assert issues
    assert all(SECRET not in issue.message for issue in issues)
    assert SECRET not in log.text


def test_an_override_that_cannot_apply_is_not_quoted(log):
    apply_overrides_with_prefix(
        _Config(), {"input_modules.mod.token": [SECRET]}, "input_modules.mod."
    )

    assert "input_modules.mod.token" in log.text
    assert SECRET not in log.text


def test_reconciling_a_credential_a_plugin_changed_logs_it_redacted(log):
    overrides = {"input_modules.mod.token": "old-" + SECRET}

    changed = PreparedModuleCollection()._reconcile_consumed_overrides(
        "mod", _Plugin, _Config(token="new-" + SECRET), overrides
    )

    assert changed == 1
    assert overrides == {"input_modules.mod.token": "new-" + SECRET}
    assert SECRET not in log.text


def test_a_required_credential_with_a_default_is_warned_about_redacted(log):
    class _Required(ModuleConfig):
        name: str = Field(default="req", frozen=True, exclude=True)
        key: str = Field(
            default=SECRET, json_schema_extra={**_PASSWORD, "setup": "required"}
        )

    build_presentation(KalinkaConfig(), {"req": _Required()}, {})

    assert "setup=required" in log.text
    assert SECRET not in log.text
