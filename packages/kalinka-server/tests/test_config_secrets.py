"""Credentials in the configuration never leave the server in plain text.

Contract:

* A field with the password widget is a credential. ``GET /server/config``
  never carries its value — only whether it is set — and the schema never
  carries its default.
* No log line the server writes about configuration shows a credential:
  not the save, not the reconcile after plugin setup, not an override that
  fails to apply, not a default the emitter warns about.
* A credential the type refuses is refused without being quoted back.
* A field declared ``private`` — a host, a path, a user name — is sent to
  clients as usual, but a log only ever names it and says it was updated.
* An exported log leaves out, whole, every line that holds a credential the
  configuration holds or looks like it carries one: a URL password, or a
  credential-named header, query parameter or key-value pair.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
from kalinka_server.config_secrets import (
    REDACTED,
    credential_detector,
    is_private_path,
    loggable,
    secret_values,
)
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


def test_saving_a_credential_logs_only_that_it_was_updated(log):
    config = _Config()
    targets = _targets(config)

    for key in ("input_modules.mod.token", "input_modules.mod.login.password"):
        assert commit_change(targets.resolve(key), key, SECRET) is None

    assert config.token == SECRET
    assert config.login.password == SECRET
    assert SECRET not in log.text
    assert REDACTED not in log.text
    assert "Updated config field input_modules.mod.token" in log.text
    assert "Updated config field input_modules.mod.login.password" in log.text


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


def test_every_configured_credential_is_found_nested_groups_included():
    config = _Config(token="tok-1234", login=_Login(password="pw-5678"))

    assert sorted(secret_values(config)) == ["pw-5678", "tok-1234"]


def test_an_unset_credential_is_not_a_value_to_look_for():
    assert list(secret_values(_Config())) == []


@pytest.mark.parametrize(
    "line",
    [
        f"login failed for {SECRET}",
        "GET /x?q=p%40ss%20word%2F1 HTTP/1.1",
        "body=p%40ss+word%2F1",
    ],
)
def test_a_line_holding_a_configured_credential_is_flagged(line):
    assert credential_detector([SECRET, "p@ss word/1"])(line)


@pytest.mark.parametrize(
    "line",
    [
        "mounting smb://guest:s3cret/x@nas/music",
        "connecting_to_the_remote_nas_via_https://user:pw@nas/x",
        "Authorization: Bearer abc.def.ghi",
        "headers={'authorization': 'Basic dXNlcjpwdw=='}",
        "Proxy-Authorization: tok123",
        "X-User-Auth-Token: u-tok",
        "Cookie: session=abc",
        "GET https://api.example/v1?key=AIzaSy123&q=jazz",
        "GET /track/getFileUrl?user_auth_token=u-tok&request_sig=abcd",
        "https://x/y?accessToken=zz9",
        "https://x/y?APIKey=zz9",
        "login failed: password=hunter3",
        "{'password': 'hunter3'}",
        '{"client_secret": "zz9"}',
    ],
)
def test_a_line_that_looks_like_it_carries_a_credential_is_flagged(line):
    assert credential_detector([])(line)


@pytest.mark.parametrize(
    "line",
    [
        "2026-09-23T10:00:00.123Z kalinka.service [info] server: Starting on 0.0.0.0:8000",
        "GET /search?q=miles+davis&limit=10&keyword=jazz&tokenType=bearer HTTP/1.1",
        "cache key=album:42 missed",
        "enricher: 12 of 40 tokens matched",
        "Setting config field input_modules.localfiles.smb.password to <secret>",
        "Setting config field x.url to 'smb://guest:<secret>@nas/music'",
        "password=<secret> token: null secret=***",
        "ssh://git@github.com/owner/repo",
    ],
)
def test_an_ordinary_line_is_not_flagged(line):
    assert not credential_detector([SECRET])(line)


@pytest.mark.parametrize(
    "line",
    [
        "q" * 65536,
        "a." * 32768,
        "a." * 32768 + "://u:p@h",
        "a://" * 16384,
        "@" + "a://a:" * 10922,
        "a" * 65536 + "=",
        "?" + "a" * 65536,
    ],
)
def test_a_record_at_the_size_cap_is_checked_without_rescanning_it(line):
    carries_credential = credential_detector([SECRET])
    started = time.monotonic()

    carries_credential(line)

    assert time.monotonic() - started < 1.0


def test_a_url_password_is_masked_after_a_long_word_too():
    line = "connecting_to_the_remote_nas_via_https://user:pw@nas/x"

    assert ":pw@" not in loggable(line)


_PRIVATE = {"private": True}
HOST = "nas.alice-home.lan"


class _Share(BaseModel):
    host: str = Field(default="", json_schema_extra=_PRIVATE)
    port: int = 445


class _Where(BaseModel):
    folder: str = ""


class _PrivateConfig(ModuleConfig):
    name: str = Field(default="priv", frozen=True, exclude=True)
    share: _Share = Field(default_factory=_Share)
    where: _Where = Field(default_factory=_Where, json_schema_extra=_PRIVATE)
    port: int = 8000


class _PrivatePlugin(_Plugin):
    PLUGIN_ID = "priv"
    CONFIG_MODEL = _PrivateConfig


def _private_targets(config: _PrivateConfig) -> ConfigTargets:
    return ConfigTargets(
        base_config=KalinkaConfig(),
        input_modules={"priv": _Prepared(_Context(config))},
        devices={},
    )


@pytest.mark.parametrize(
    "key, value",
    [
        ("input_modules.priv.share.host", HOST),
        ("input_modules.priv.share", {"host": HOST, "port": 445}),
        ("input_modules.priv.where.folder", f"/home/{HOST}"),
        ("input_modules.priv.where", {"folder": f"/home/{HOST}"}),
    ],
)
def test_saving_a_private_field_logs_its_path_and_nothing_of_its_value(log, key, value):
    targets = _private_targets(_PrivateConfig())

    assert commit_change(targets.resolve(key), key, value) is None

    assert HOST not in log.text
    assert f"Updated config field {key}" in log.text


def test_a_private_field_is_still_sent_to_clients():
    known = _values(priv=_PrivateConfig(share=_Share(host=HOST)))

    assert known.values["input_modules.priv.share.host"] == HOST


def test_the_fields_beside_a_private_one_still_log_their_value(log):
    targets = _private_targets(_PrivateConfig())
    key = "input_modules.priv.share.port"

    commit_change(targets.resolve(key), key, 4450)

    assert "4450" in log.text


def test_reconciling_a_private_field_logs_its_path_only(log):
    overrides = {"input_modules.priv.share.host": "old-" + HOST}

    changed = PreparedModuleCollection()._reconcile_consumed_overrides(
        "priv", _PrivatePlugin, _PrivateConfig(share=_Share(host="new-" + HOST)), overrides
    )

    assert changed == 1
    assert HOST not in log.text
    assert "Reconciled override input_modules.priv.share.host" in log.text


def test_a_required_private_field_with_a_default_is_warned_about_by_name(log):
    class _Required(ModuleConfig):
        name: str = Field(default="req", frozen=True, exclude=True)
        host: str = Field(
            default=HOST, json_schema_extra={**_PRIVATE, "setup": "required"}
        )

    build_presentation(KalinkaConfig(), {"req": _Required()}, {})

    assert "input_modules.req.host" in log.text
    assert HOST not in log.text


@pytest.mark.parametrize(
    "attrs, private",
    [
        (["share", "host"], True),
        (["share"], True),
        (["where", "folder"], True),
        (["share", "port"], False),
        (["port"], False),
        (["no_such_field"], True),
    ],
)
def test_what_counts_as_private(attrs, private):
    assert is_private_path(_PrivateConfig, attrs) is private
