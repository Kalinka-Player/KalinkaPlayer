"""Where a module's settings sit relative to one another on its page.

Contract:

* A section comes before the scalar declared after it, named in ``before``;
  one declared after every scalar has none, and sits after all of them as
  it always has.
* A view that hides the field a section came before hands the place to the
  next field it shows, or to the end.
* A section of a model laid out inline keeps its place among the fields
  that model's parent declares.
* A field's ``heading`` opens a group on a page that lays settings out in
  groups.
* A collection's ``status`` names the read-only field reporting on its
  entries, by full path.
* A dynamic field of a model laid out inline lands among that model's fields
  in the section holding them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kalinka_plugin_sdk import ConfigRecord, DynamicFieldDecl, Records
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import (
    _inject_dynamic_fields,
    _module_spec,
    build_presentation,
)
from kalinka_server.dynamic_field_registry import DynamicFieldEntry
from kalinka_server.presentation_schema import (
    FieldSpec,
    ModuleSpec,
    SectionSpec,
    Widget,
)

_SIMPLE = {"importance": "simple"}


class _Feature(BaseModel):
    enabled: bool = Field(default=False, json_schema_extra=_SIMPLE)


class _Tail(BaseModel):
    on: bool = Field(default=False, json_schema_extra=_SIMPLE)
    extra: _Feature = Field(default_factory=_Feature, title="Extra")


class _Folder(ConfigRecord):
    path: str = ""


class _Config(ModuleConfig):
    name: str = Field(default="mod", frozen=True, exclude=True)
    enabled: bool = Field(default=True, json_schema_extra=_SIMPLE)
    folders: Records[_Folder] = Field(
        default_factory=list,
        title="Folders",
        json_schema_extra={"status": "folders_status"},
    )
    early: _Feature = Field(default_factory=_Feature, title="Early")
    tucked_away: int = 0
    shown: int = Field(default=0, json_schema_extra={"heading": "Library", **_SIMPLE})
    tail: _Tail = Field(default_factory=_Tail, json_schema_extra={"inline": True})
    after_tail: int = Field(default=0, json_schema_extra=_SIMPLE)
    last: _Feature = Field(default_factory=_Feature, title="Last")


def _modules() -> tuple[ModuleSpec, ModuleSpec]:
    """The module as the page shows it, and with every field."""
    page = build_presentation(KalinkaConfig(), {"mod": _Config()}, {})
    return page.pages[1].modules[0], _module_spec(
        _Config(), "input_module", path_prefix="input_modules"
    )


def _section(module: ModuleSpec, name: str) -> SectionSpec:
    return next(s for s in module.sections if s.id == f"input_modules.mod.{name}")


class TestWhereASectionSits:
    def test_before_the_scalar_declared_after_it(self):
        _, full = _modules()
        assert _section(full, "early").before == "input_modules.mod.tucked_away"

    def test_after_everything_when_declared_last(self):
        shown, full = _modules()
        assert _section(full, "last").before is None
        assert _section(shown, "last").before is None

    def test_a_hidden_field_hands_its_place_to_the_next_shown_one(self):
        shown, _ = _modules()
        assert _section(shown, "early").before == "input_modules.mod.shown"

    def test_an_inline_model_keeps_its_sections_among_its_parents_fields(self):
        _, full = _modules()
        assert _section(full, "tail.extra").before == "input_modules.mod.after_tail"


def test_a_heading_rides_on_the_field_that_opens_the_group():
    shown, _ = _modules()
    headings = {f.path: f.heading for f in shown.fields}
    assert headings["input_modules.mod.shown"] == "Library"
    assert headings["input_modules.mod.enabled"] is None


def test_a_hidden_field_hands_its_heading_to_the_next_shown_one():
    class _Headed(ModuleConfig):
        name: str = Field(default="mod", frozen=True, exclude=True)
        enabled: bool = Field(default=True, json_schema_extra=_SIMPLE)
        tucked: int = Field(default=0, json_schema_extra={"heading": "Library"})
        shown: int = Field(default=0, json_schema_extra=_SIMPLE)

    page = build_presentation(KalinkaConfig(), {"mod": _Headed()}, {})
    headings = {f.path: f.heading for f in page.pages[1].modules[0].fields}
    assert headings["input_modules.mod.shown"] == "Library"


def test_a_collection_names_its_status_field_by_full_path():
    shown, _ = _modules()
    assert shown.collections[0].status == "input_modules.mod.folders_status"


def test_a_dynamic_field_of_an_inline_model_lands_beside_its_fields():
    def field(path: str) -> FieldSpec:
        return FieldSpec(path=path, label=path, widget=Widget.TEXT, type="str")

    section = SectionSpec(
        id="input_modules.x.enricher",
        title="Enricher",
        fields=[
            field("input_modules.x.enricher.enabled"),
            field("input_modules.x.enricher.plugins.key.api_key"),
            field("input_modules.x.enricher.plugins.art.enabled"),
        ],
    )
    entry = DynamicFieldEntry(
        full_path="input_modules.x.key.status_view",
        plugin_id="x",
        kind="input_module",
        plugin_instance=None,
        subpath="key.status_view",
        decl=DynamicFieldDecl(section_id="enricher.plugins.key", label="Status"),
    )
    _inject_dynamic_fields([section], "input_modules.x", [entry])
    assert [f.path for f in section.fields] == [
        "input_modules.x.enricher.enabled",
        "input_modules.x.enricher.plugins.key.api_key",
        "input_modules.x.key.status_view",
        "input_modules.x.enricher.plugins.art.enabled",
    ]


def test_my_library_reads_sources_then_ai_search_then_metadata_then_library():
    from kalinka_plugin_localfiles.config_model import LocalFilesConfig
    from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles

    prefix = "input_modules.localfiles."
    registry = {
        prefix + sub: DynamicFieldEntry(
            full_path=prefix + sub,
            plugin_id="localfiles",
            kind="input_module",
            plugin_instance=None,
            subpath=sub,
            decl=decl,
        )
        for sub, decl in KalinkaPluginLocalFiles.DYNAMIC_FIELDS.items()
    }
    schema = build_presentation(
        KalinkaConfig(),
        {"localfiles": LocalFilesConfig()},
        {},
        dynamic_field_registry=registry,
    )
    module = schema.pages[1].modules[0]
    short = lambda path: path.removeprefix(prefix)  # noqa: E731

    assert [(short(s.id), short(s.before)) for s in module.sections] == [
        ("ai_search", "scan_interval_minutes"),
        ("enricher", "scan_interval_minutes"),
    ]
    assert short(module.collections[0].status) == "storage.status_view"
    enricher = module.sections[1]
    assert enricher.sections == []
    assert [short(f.path) for f in enricher.fields] == [
        "enricher.enabled",
        "enricher.status_view",
        "enricher.plugins.acoustid.api_key",
        "acoustid.status_view",
        "enricher.plugins.procedural_artwork.enabled",
    ]
