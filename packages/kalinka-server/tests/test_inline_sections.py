"""Tests for nested config models tagged ``inline`` in the presentation schema.

Contract:

* An inline model gets no section of its own: its fields join the parent's,
  at the position the model is declared, not after the parent's scalars.
* Only the presentation moves — field paths still name the nested model,
  so stored config and the expert list are unchanged.
* The inline model's own nested models still become sections.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_presentation

_SIMPLE = {"importance": "simple"}


class _Tuning(BaseModel):
    depth: int = Field(default=1, json_schema_extra=_SIMPLE)


class _Credentials(BaseModel):
    user: str = Field(default="", json_schema_extra=_SIMPLE)
    secret: str = Field(default="", json_schema_extra=_SIMPLE)
    tuning: _Tuning = Field(default_factory=_Tuning, title="Tuning")


class _Group(BaseModel):
    first: int = Field(default=0, json_schema_extra=_SIMPLE)
    login: _Credentials = Field(
        default_factory=_Credentials, json_schema_extra={"inline": True}
    )
    last: int = Field(default=0, json_schema_extra=_SIMPLE)


class _Module(ModuleConfig):
    name: str = Field(default="mod", title="Mod", frozen=True, exclude=True)
    enabled: bool = Field(default=True, json_schema_extra=_SIMPLE)
    folders: list[str] = Field(default_factory=list, json_schema_extra=_SIMPLE)
    login: _Credentials = Field(
        default_factory=_Credentials, json_schema_extra={"inline": True}
    )
    interval: int = Field(default=5, json_schema_extra=_SIMPLE)
    group: _Group = Field(default_factory=_Group, title="Group")


def _schema(modules):
    return build_presentation(
        base_config=KalinkaConfig(), input_modules=modules, devices={}
    )


def _module(schema, module_id):
    return next(
        m for p in schema.pages for m in p.modules if m.id == module_id
    )


def test_inline_fields_sit_where_the_model_is_declared():
    module = _module(_schema({"mod": _Module()}), "mod")

    assert [f.path for f in module.fields] == [
        "input_modules.mod.enabled",
        "input_modules.mod.folders",
        "input_modules.mod.login.user",
        "input_modules.mod.login.secret",
        "input_modules.mod.interval",
    ]


def test_inline_model_has_no_section_but_its_children_do():
    module = _module(_schema({"mod": _Module()}), "mod")

    assert [s.id for s in module.sections] == [
        "input_modules.mod.login.tuning",
        "input_modules.mod.group",
    ]


def test_inline_inside_a_section_joins_that_section():
    module = _module(_schema({"mod": _Module()}), "mod")
    group = next(s for s in module.sections if s.id == "input_modules.mod.group")

    assert [f.path for f in group.fields] == [
        "input_modules.mod.group.first",
        "input_modules.mod.group.login.user",
        "input_modules.mod.group.login.secret",
        "input_modules.mod.group.last",
    ]
    assert [s.id for s in group.sections] == [
        "input_modules.mod.group.login.tuning"
    ]


def test_inline_fields_stay_in_the_expert_list():
    paths = {f.path for f in _schema({"mod": _Module()}).expert_fields}

    assert "input_modules.mod.login.user" in paths
    assert "input_modules.mod.login.tuning.depth" in paths
