"""A part of a setting that takes one of several shapes, written without
saying which.

Contract:

* The schema tells a client the shape such a part takes while its
  discriminator is unset (``GroupSpec.default``), so a client may leave the
  discriminator out, or send it null. A write that does takes that shape,
  in an entry of a list or not, in the dry run, the save and a restart that
  reads the overrides file alike.
* A discriminator that is given is kept, whatever the default.
* Where no default is declared nothing is guessed: an entry of a list
  written without its kind is refused, and so is a part with no default.
* What the client sent is left as it was, since the save reads it again to
  see which credentials it left out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from kalinka_plugin_localfiles.config_model import AccountSignIn, LocalFilesConfig
from kalinka_plugin_sdk import ConfigRecord, Records
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix
from kalinka_server.config_schema_processor import build_presentation
from kalinka_server.config_validation import (
    ConfigTargets,
    apply_change,
    validate_changes,
)


class _Account(BaseModel):
    mode: Literal["account"] = "account"
    user: str = ""
    password: str = Field(default="", json_schema_extra={"widget": "password"})


class _Guest(BaseModel):
    mode: Literal["guest"] = "guest"


class _Share(ConfigRecord):
    kind: Literal["share", "smb"] = "share"
    host: str = ""
    login: Union[_Guest, _Account] = Field(
        default_factory=_Account, discriminator="mode"
    )


class _Folder(ConfigRecord):
    kind: Literal["folder"] = "folder"
    path: str = ""


_Entry = Annotated[Union[_Share, _Folder], Field(discriminator="kind")]


class _Config(ModuleConfig):
    name: str = Field(default="mod", frozen=True, exclude=True)
    entries: Records[_Entry] = Field(default_factory=list)
    login: Union[_Guest, _Account] = Field(
        default_factory=_Account, discriminator="mode"
    )


class _NoDefault(BaseModel):
    login: Optional[Union[_Guest, _Account]] = Field(
        default=None, discriminator="mode"
    )


@dataclass
class _Context:
    config: BaseModel


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any = None


def _share(**login) -> dict:
    return {"id": "nas", "kind": "share", "host": "nas", "login": login}


def _write(config: BaseModel, attrs: list[str], value: Any) -> str | None:
    return apply_change(config, attrs, value).refused


class TestAPartLeftUnset:
    def test_takes_the_shape_its_field_defaults_to(self):
        config = _Config()
        entry = _share(user="media", password="pw")
        assert _write(config, ["entries"], [entry]) is None
        assert config.entries[0].login == _Account(user="media", password="pw")

    def test_or_sent_null(self):
        config = _Config()
        assert _write(config, ["entries"], [_share(mode=None, user="media")]) is None
        assert config.entries[0].login == _Account(user="media")

    def test_in_an_entry_named_by_any_of_its_tags(self):
        config = _Config()
        entry = {**_share(user="media"), "kind": "smb"}
        assert _write(config, ["entries"], [entry]) is None
        assert config.entries[0].login == _Account(user="media")

    def test_outside_a_list_too(self):
        config = _Config(login=_Guest())
        assert _write(config, ["login"], {}) is None
        assert config.login == _Account()

    def test_is_the_shape_the_schema_names_as_its_default(self):
        schema = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})
        [collection] = schema.pages[1].modules[0].collections
        share = next(v for v in collection.variants if v.key == "share")
        login = next(g for g in share.groups if g.path == "login")
        assert login.default == "account"

    def test_the_dry_run_agrees(self):
        targets = ConfigTargets(
            base_config=KalinkaConfig(),
            input_modules={"mod": _Prepared(_Context(_Config()))},
            devices={},
        )
        changes = {"input_modules.mod.entries": [_share(user="media")]}
        assert asyncio.run(validate_changes(changes, targets)) == []

    def test_a_restart_reads_it_the_same_way(self):
        config = _Config()
        overrides = {"input_modules.mod.entries": [_share(user="media")]}
        apply_overrides_with_prefix(config, overrides, "input_modules.mod.")
        assert config.entries[0].login == _Account(user="media")


class TestWhatIsNotFilledIn:
    def test_a_discriminator_that_is_given_is_kept(self):
        config = _Config()
        _write(config, ["entries"], [_share(mode="guest")])
        assert config.entries[0].login == _Guest()

    def test_an_entry_without_its_kind_is_refused(self):
        config = _Config()
        assert _write(config, ["entries"], [{"id": "nas", "host": "nas"}]) is not None
        assert config.entries == []

    def test_a_part_without_a_default_has_to_say_what_it_is(self):
        config = _NoDefault()
        assert _write(config, ["login"], {}) is not None
        assert config.login is None

    def test_what_the_client_sent_is_left_as_it_was(self):
        sent = [_share(user="media")]
        _write(_Config(), ["entries"], sent)
        assert sent == [_share(user="media")]


class TestAShareAddedInTheApp:
    def test_signs_in_with_the_account_typed_into_it(self):
        """Regression: 5.1.0 refused this with "Unable to extract tag using
        discriminator 'mode'", the way the app sends a new share whose Sign
        in was left on the Account it shows."""
        config = LocalFilesConfig()
        share = {
            "id": "rec_0123456789ab",
            "kind": "smb",
            "location": {"host": "192.168.0.220", "path": "Music"},
            "authentication": {"username": "media", "password": "pw"},
        }
        assert _write(config, ["music_sources"], [share]) is None
        assert config.music_sources[0].authentication == AccountSignIn(
            username="media", password="pw"
        )
