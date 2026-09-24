"""A list of records, described to the settings page as a collection.

Contract:

* A ``Records[...]`` field becomes a collection beside the fields of what
  holds it, never an expert field: a client shows its entries as cards and
  opens one in a dialog.
* Each shape an entry can take is a variant, keyed by the value of its
  discriminator and labelled, explained and drawn by that field's
  declaration. The record's id and discriminator are not fields to edit.
* Inside an entry, a nested model is a group and a discriminated union a
  group with variants; every path is relative to the entry.
* A collection sits after the field declared before it, and a view that
  hides that field moves it up to the nearest one it shows. It is kept in
  the simple view whatever surrounds it.
* A collection holding what older fields hold names them in ``replaces``,
  by full path. Those fields stay described: a client that shows the
  collection hides them, and one that cannot ignores the list.
* A list of models that are not records is not described at all.
* Suggestions for a field inside an entry are bound under
  ``<collection path>.<field path>``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, Field

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_sdk import ConfigRecord, Records
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_presentation
from kalinka_server.options_registry import OptionsRegistry, register_plugin_options
from kalinka_server.presentation_schema import CollectionSpec, ModuleSpec

SECRET = "hunter2-do-not-log"
_SIMPLE = {"importance": "simple"}


class _Account(BaseModel):
    mode: Literal["account"] = Field(default="account", title="Account")
    user: str = Field(default="", title="User", json_schema_extra=_SIMPLE)
    password: str = Field(
        default=SECRET, title="Password", json_schema_extra={"widget": "password"}
    )


class _Guest(BaseModel):
    mode: Literal["guest"] = Field(default="guest", title="Guest")


class _Address(BaseModel):
    host: str = Field(
        default="", title="Host", json_schema_extra={"dynamic_options": True}
    )
    port: int = Field(default=445, title="Port")


class _Server(ConfigRecord):
    __preview_fields__: ClassVar[list[str]] = ["address.host"]

    kind: Literal["server"] = Field(
        default="server",
        title="Server",
        description="Somewhere on the network",
        json_schema_extra={"icon": "lan_outlined"},
    )
    label: str = Field(default="", title="Label")
    address: _Address = Field(default_factory=_Address, title="Address")
    login: Union[_Guest, _Account] = Field(
        default_factory=_Guest, discriminator="mode", title="Sign in"
    )


class _Folder(ConfigRecord):
    kind: Literal["folder"] = Field(default="folder", title="Folder")
    path: str = Field(default="", title="Path")


class _Single(ConfigRecord):
    path: str = ""


class _Inline(BaseModel):
    lead: str = Field(default="", json_schema_extra=_SIMPLE)
    inner: Records[_Single] = Field(default_factory=list, title="Inner")


class _Nested(BaseModel):
    tuning: int = 3
    deep: Records[_Single] = Field(
        default_factory=list, title="Deep", json_schema_extra={"replaces": ["tuning"]}
    )


_Entry = Annotated[Union[_Server, _Folder], Field(discriminator="kind")]


class _Config(ModuleConfig):
    name: str = Field(default="mod", frozen=True, exclude=True)
    first: str = Field(default="", json_schema_extra=_SIMPLE)
    tucked_away: int = 0
    entries: Records[_Entry] = Field(
        default_factory=list, title="Entries", json_schema_extra={"help": "Where"}
    )
    plain: list[_Account] = Field(default_factory=list)
    last: str = Field(default="", json_schema_extra=_SIMPLE)
    singles: Records[_Single] = Field(
        default_factory=list, title="Singles", json_schema_extra={"replaces": ["last"]}
    )
    nested: _Nested = Field(default_factory=_Nested, title="Nested")
    inline: _Inline = Field(default_factory=_Inline, json_schema_extra={"inline": True})


def _module(config=None) -> ModuleSpec:
    schema = build_presentation(KalinkaConfig(), {"mod": config or _Config()}, {})
    return schema.pages[1].modules[0]


def _collection(module: ModuleSpec, name: str) -> CollectionSpec:
    return next(c for c in module.collections if c.path.endswith(f".{name}"))


def _variant(collection: CollectionSpec, key: str):
    return next(v for v in collection.variants if v.key == key)


class TestWhereACollectionIsDescribed:
    def test_beside_the_fields_of_its_module_and_not_among_them(self):
        module = _module()
        schema = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})

        assert [c.path for c in module.collections] == [
            "input_modules.mod.entries",
            "input_modules.mod.singles",
            "input_modules.mod.inline.inner",
        ]
        assert not any(".entries" in f.path for f in schema.expert_fields)
        assert not any(".entries" in f.path for f in module.fields)

    def test_a_list_of_models_that_are_not_records_is_not_described(self):
        schema = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})
        assert ".plain" not in schema.model_dump_json()

    def test_inside_a_nested_section_it_joins_that_section(self):
        section = next(s for s in _module().sections if s.id.endswith(".nested"))
        assert [c.path for c in section.collections] == [
            "input_modules.mod.nested.deep"
        ]

    def test_it_is_kept_where_nothing_else_is_simple(self):
        """The expert list has no editor for one: the page view is its only
        way in."""
        section = next(s for s in _module().sections if s.id.endswith(".nested"))
        assert section.fields == []
        assert section.collections


class TestWhereItSits:
    def test_after_the_field_declared_before_it(self):
        full = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})
        # The expert list keeps every field; the anchor is set from the
        # declaration, before the page view prunes anything.
        assert "input_modules.mod.tucked_away" in {f.path for f in full.expert_fields}
        assert _collection(_module(), "singles").after == "input_modules.mod.last"

    def test_a_hidden_field_hands_its_place_to_the_nearest_shown_one(self):
        assert _collection(_module(), "entries").after == "input_modules.mod.first"

    def test_first_of_all_when_nothing_before_it_is_shown(self):
        section = next(s for s in _module().sections if s.id.endswith(".nested"))
        assert section.collections[0].after is None

    def test_one_promoted_from_an_inline_group_keeps_its_place(self):
        assert _collection(_module(), "inner").after == "input_modules.mod.inline.lead"


class TestWhatItReplaces:
    def test_the_older_fields_by_full_path(self):
        module = _module()
        assert _collection(module, "singles").replaces == ["input_modules.mod.last"]
        assert _collection(module, "entries").replaces == []

    def test_inside_a_section_they_are_its_siblings(self):
        [section] = [s for s in _module().sections if s.collections]
        [deep] = section.collections
        assert deep.replaces == ["input_modules.mod.nested.tuning"]

    def test_they_stay_described_for_an_app_that_cannot_show_it(self):
        paths = [f.path for f in _module().fields]
        assert "input_modules.mod.last" in paths


class TestTheShapesOfAnEntry:
    def test_each_is_keyed_and_labelled_by_its_discriminator(self):
        entries = _collection(_module(), "entries")
        server = _variant(entries, "server")

        assert entries.discriminator == "kind"
        assert [v.key for v in entries.variants] == ["server", "folder"]
        assert (server.label, server.description, server.icon) == (
            "Server",
            "Somewhere on the network",
            "lan_outlined",
        )
        assert entries.title == "Entries" and entries.help == "Where"

    def test_the_card_is_summarised_as_the_record_declares(self):
        server = _variant(_collection(_module(), "entries"), "server")
        assert server.summary == ["address.host"]

    def test_the_id_and_the_discriminator_are_not_fields_to_edit(self):
        server = _variant(_collection(_module(), "entries"), "server")
        assert [f.path for f in server.fields] == ["label"]

    def test_a_nested_model_is_a_group_with_paths_inside_the_entry(self):
        server = _variant(_collection(_module(), "entries"), "server")
        address = next(g for g in server.groups if g.path == "address")
        assert address.title == "Address"
        assert [f.path for f in address.fields] == ["address.host", "address.port"]

    def test_a_union_is_a_group_of_shapes_starting_from_its_default(self):
        server = _variant(_collection(_module(), "entries"), "server")
        login = next(g for g in server.groups if g.path == "login")

        assert (login.title, login.discriminator, login.default) == (
            "Sign in",
            "mode",
            "guest",
        )
        assert [(v.key, v.label) for v in login.variants] == [
            ("guest", "Guest"),
            ("account", "Account"),
        ]
        account = login.variants[1]
        assert [f.path for f in account.fields] == ["login.user", "login.password"]

    def test_a_credential_inside_carries_no_default(self):
        schema = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})
        assert SECRET not in schema.model_dump_json()

    def test_a_record_of_one_shape_is_one_variant_named_after_the_list(self):
        singles = _collection(_module(), "singles")
        assert singles.discriminator is None
        assert [(v.key, v.label) for v in singles.variants] == [("", "Singles")]
        assert [f.path for f in singles.variants[0].fields] == ["path"]


@dataclass
class _Context:
    config: BaseModel


class _Suggesting:
    def __init__(self):
        self.asked = []

    async def resolve_options(self, path):
        self.asked.append(path)
        return [{"value": "nas", "label": "NAS"}]


@dataclass
class _Prepared:
    plugin_context: _Context
    plugin_instance: Any = None


class TestSuggestionsInsideAnEntry:
    def test_are_bound_under_the_collection_and_the_field(self):
        plugin = _Suggesting()
        registry = OptionsRegistry()
        schema = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})
        register_plugin_options(
            registry, schema, {"mod": _Prepared(_Context(_Config()), plugin)}, {}
        )

        assert registry.paths() == ["input_modules.mod.entries.address.host"]
        [option] = asyncio.run(registry.resolve(registry.paths()[0]))
        assert option.value == "nas"
        assert plugin.asked == ["entries.address.host"]


class TestTheLocalLibrary:
    def _sources(self) -> CollectionSpec:
        schema = build_presentation(
            KalinkaConfig(), {"localfiles": LocalFilesConfig()}, {}
        )
        module = schema.pages[1].modules[0]
        return _collection(module, "music_sources")

    def test_sources_take_the_folders_place(self):
        sources = self._sources()
        assert sources.after == "input_modules.localfiles.music_folders"
        assert sources.replaces == ["input_modules.localfiles.music_folders"]

    def test_a_source_is_a_folder_or_a_share(self):
        assert [(v.key, v.label) for v in self._sources().variants] == [
            ("local", "Folder on the server"),
            ("smb", "Network share"),
        ]

    def test_a_share_is_laid_out_as_location_sign_in_and_options(self):
        share = _variant(self._sources(), "smb")
        assert share.fields == []
        assert [g.path for g in share.groups] == [
            "location",
            "authentication",
            "options",
        ]
        assert share.summary == ["location.host", "location.path"]
