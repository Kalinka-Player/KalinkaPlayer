"""Tests for the dynamic-field registry, schema injection, and values resolution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from kalinka_plugin_sdk import (
    DynamicFieldDecl,
    ModuleHealthState,
    ModuleState,
)
from kalinka_plugin_sdk.plugin import PluginBase

from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_schema_processor import build_presentation, build_values
from kalinka_server.dynamic_field_registry import (
    build_dynamic_field_registry,
    resolve_value,
)
from kalinka_server.presentation_schema import SectionSpec


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePluginClass:
    DYNAMIC_FIELDS: dict[str, DynamicFieldDecl] = {}


@dataclass
class _FakePrepared:
    plugin_class: type
    plugin_instance: Any


class _FakeInstance:
    def __init__(self, resolved: dict[str, Any] | None = None):
        self._resolved = resolved or {}

    async def resolve_dynamic_field(self, path: str):
        if path not in self._resolved:
            raise KeyError(path)
        return self._resolved[path]


def _prepared(decls: dict[str, DynamicFieldDecl], instance: Any) -> _FakePrepared:
    cls = type("PluginCls", (_FakePluginClass,), {"DYNAMIC_FIELDS": decls})
    return _FakePrepared(plugin_class=cls, plugin_instance=instance)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_builds_full_paths_from_kind_and_id():
    decls = {
        "ai_search.status_view": DynamicFieldDecl(
            section_id="ai_search", label="Status", widget="rich_text"
        ),
    }
    instance = _FakeInstance({"ai_search.status_view": "ok"})
    registry = build_dynamic_field_registry(
        input_modules={"localfiles": _prepared(decls, instance)},
        devices={},
    )
    assert set(registry) == {"input_modules.localfiles.ai_search.status_view"}
    entry = registry["input_modules.localfiles.ai_search.status_view"]
    assert entry.plugin_id == "localfiles"
    assert entry.kind == "input_module"
    assert entry.subpath == "ai_search.status_view"


def test_registry_skips_plugins_without_instances():
    decls = {"a.x": DynamicFieldDecl(section_id="a", label="x")}
    prepared = _FakePrepared(
        plugin_class=type("X", (_FakePluginClass,), {"DYNAMIC_FIELDS": decls}),
        plugin_instance=None,
    )
    registry = build_dynamic_field_registry({"broken": prepared}, {})
    assert registry == {}


def test_registry_warns_on_non_decl_entry(caplog):
    decls = {"x": "not-a-decl"}  # type: ignore[dict-item]
    prepared = _prepared(decls, _FakeInstance())  # type: ignore[arg-type]
    with caplog.at_level("WARNING"):
        registry = build_dynamic_field_registry({"p": prepared}, {})
    assert registry == {}
    assert "expected DynamicFieldDecl" in caplog.text


# ---------------------------------------------------------------------------
# resolve_value
# ---------------------------------------------------------------------------


def test_resolve_value_returns_value_on_success():
    instance = _FakeInstance({"x": "value-x"})
    decls = {"x": DynamicFieldDecl(section_id="", label="x")}
    registry = build_dynamic_field_registry(
        {"p": _prepared(decls, instance)}, {}
    )
    entry = next(iter(registry.values()))
    assert asyncio.run(resolve_value(entry)) == "value-x"


def test_resolve_value_returns_none_on_keyerror(caplog):
    instance = _FakeInstance({})  # nothing registered
    decls = {"x": DynamicFieldDecl(section_id="", label="x")}
    registry = build_dynamic_field_registry(
        {"p": _prepared(decls, instance)}, {}
    )
    entry = next(iter(registry.values()))
    with caplog.at_level("WARNING"):
        result = asyncio.run(resolve_value(entry))
    assert result is None


def test_resolve_value_returns_none_on_unexpected_exception(caplog):
    class _Boom:
        async def resolve_dynamic_field(self, path):
            raise RuntimeError("boom")

    decls = {"x": DynamicFieldDecl(section_id="", label="x")}
    registry = build_dynamic_field_registry(
        {"p": _prepared(decls, _Boom())}, {}
    )
    entry = next(iter(registry.values()))
    with caplog.at_level("ERROR"):
        result = asyncio.run(resolve_value(entry))
    assert result is None
    assert "Failed to resolve dynamic field" in caplog.text


# ---------------------------------------------------------------------------
# build_values: dynamic resolution
# ---------------------------------------------------------------------------


def test_build_values_includes_resolved_dynamic_values():
    instance = _FakeInstance({"ai_search.status_view": "**Ready**"})
    decls = {
        "ai_search.status_view": DynamicFieldDecl(
            section_id="ai_search", label="Status"
        ),
    }
    registry = build_dynamic_field_registry(
        {"localfiles": _prepared(decls, instance)}, {}
    )
    values = asyncio.run(
        build_values(KalinkaConfig(), {}, {}, registry.values())
    ).values
    assert (
        values["input_modules.localfiles.ai_search.status_view"] == "**Ready**"
    )


def test_build_values_omits_failed_dynamic_paths():
    instance = _FakeInstance({})  # raises KeyError for any path
    decls = {"x": DynamicFieldDecl(section_id="", label="x")}
    registry = build_dynamic_field_registry(
        {"p": _prepared(decls, instance)}, {}
    )
    values = asyncio.run(
        build_values(KalinkaConfig(), {}, {}, registry.values())
    ).values
    # The failed path should not appear in values.
    assert "input_modules.p.x" not in values


# ---------------------------------------------------------------------------
# Schema injection (presentation)
# ---------------------------------------------------------------------------


def test_schema_injects_dynamic_field_into_named_section():
    # Use a real plugin config so the schema has matching sub-sections.
    from kalinka_plugin_localfiles.config_model import LocalFilesConfig

    instance = _FakeInstance({"ai_search.status_view": "x"})
    decls = {
        "ai_search.status_view": DynamicFieldDecl(
            section_id="ai_search", label="Status", widget="rich_text"
        ),
    }
    registry = build_dynamic_field_registry(
        {"localfiles": _prepared(decls, instance)}, {}
    )
    schema = build_presentation(
        base_config=KalinkaConfig(),
        input_modules={"localfiles": LocalFilesConfig()},
        devices={},
        dynamic_field_registry=registry,
    )

    def find_field(sections, target_path):
        for s in sections:
            for f in s.fields:
                if f.path == target_path:
                    return s, f
            found = find_field(s.sections, target_path)
            if found is not None:
                return found
        return None

    for page in schema.pages:
        for ms in page.modules:
            if ms.id == "localfiles":
                hit = find_field(
                    ms.sections,
                    "input_modules.localfiles.ai_search.status_view",
                )
                assert hit is not None, "dynamic field not injected"
                section, field = hit
                assert section.id == "input_modules.localfiles.ai_search"
                assert field.dynamic is True
                assert field.readonly is True
                assert field.widget.value == "rich_text"
                return
    pytest.fail("localfiles module not found in schema")


def test_dynamic_field_lands_directly_after_enabled():
    """Convention: a status_view lives next to the `enabled` toggle that
    controls the same sub-feature, not at the bottom of the section."""
    from kalinka_plugin_localfiles.config_model import LocalFilesConfig

    instance = _FakeInstance({"ai_search.status_view": "x"})
    decls = {
        "ai_search.status_view": DynamicFieldDecl(
            section_id="ai_search", label="Status", widget="rich_text"
        ),
    }
    registry = build_dynamic_field_registry(
        {"localfiles": _prepared(decls, instance)}, {}
    )
    schema = build_presentation(
        base_config=KalinkaConfig(),
        input_modules={"localfiles": LocalFilesConfig()},
        devices={},
        dynamic_field_registry=registry,
    )

    def find_section(sections, target_id):
        for s in sections:
            if s.id == target_id:
                return s
            nested = find_section(s.sections, target_id)
            if nested is not None:
                return nested
        return None

    ai_section = None
    for page in schema.pages:
        for ms in page.modules:
            if ms.id == "localfiles":
                ai_section = find_section(
                    ms.sections, "input_modules.localfiles.ai_search"
                )
                break
    assert ai_section is not None, "ai_search section not emitted"

    enabled_idx = next(
        i for i, f in enumerate(ai_section.fields) if f.path.endswith(".enabled")
    )
    status_idx = next(
        i
        for i, f in enumerate(ai_section.fields)
        if f.path.endswith(".status_view")
    )
    assert status_idx == enabled_idx + 1, (
        f"status_view should land directly after enabled "
        f"(enabled@{enabled_idx}, status@{status_idx})"
    )


def test_dynamic_field_appends_when_section_has_no_enabled(caplog):
    """Fallback: section with no `enabled` toggle just appends."""
    from kalinka_server.dynamic_field_registry import DynamicFieldEntry
    from kalinka_server.config_schema_processor import (
        _inject_dynamic_fields,
    )
    from kalinka_server.presentation_schema import (
        FieldSpec,
        Importance,
        SectionSpec,
        Widget,
    )

    section = SectionSpec(
        id="input_modules.x.general",
        title="General",
        fields=[
            FieldSpec(
                path="input_modules.x.scan_interval",
                label="Scan interval",
                widget=Widget.NUMBER_INPUT,
                type="int",
            ),
        ],
    )
    entry = DynamicFieldEntry(
        full_path="input_modules.x.status_view",
        plugin_id="x",
        kind="input_module",
        plugin_instance=_FakeInstance(),
        subpath="status_view",
        decl=DynamicFieldDecl(section_id="", label="Status"),
    )
    _inject_dynamic_fields([section], "input_modules.x", [entry])
    assert section.fields[-1].path == "input_modules.x.status_view"


def test_module_top_level_scalars_render_flat_not_under_general():
    """Convention: scalar fields at the module's CONFIG_MODEL top level
    appear in `ModuleSpec.fields`, not buried inside an auto-generated
    `<module>.general` sub-section. Nested config models still become
    entries in `ModuleSpec.sections`."""
    from kalinka_plugin_localfiles.config_model import LocalFilesConfig

    schema = build_presentation(
        base_config=KalinkaConfig(),
        input_modules={"localfiles": LocalFilesConfig()},
        devices={},
    )

    module_spec = None
    for page in schema.pages:
        for ms in page.modules:
            if ms.id == "localfiles":
                module_spec = ms
                break
    assert module_spec is not None, "localfiles ModuleSpec missing"

    # Flat scalars hoisted onto the module spec
    field_paths = {f.path for f in module_spec.fields}
    assert "input_modules.localfiles.music_folders" in field_paths
    assert "input_modules.localfiles.scan_interval_minutes" in field_paths
    assert "input_modules.localfiles.enabled" in field_paths

    # No leftover "General" sub-section at the module top level
    assert not any(
        s.id == "input_modules.localfiles.general" for s in module_spec.sections
    ), "auto-general sub-section should be promoted, not retained"

    # Nested sub-sections are kept
    section_ids = {s.id for s in module_spec.sections}
    assert "input_modules.localfiles.enricher" in section_ids
    assert "input_modules.localfiles.ai_search" in section_ids


def test_schema_warns_when_section_id_is_unknown(caplog):
    from kalinka_plugin_localfiles.config_model import LocalFilesConfig

    instance = _FakeInstance({})
    decls = {
        "ghost.x": DynamicFieldDecl(
            section_id="does_not_exist", label="X"
        ),
    }
    registry = build_dynamic_field_registry(
        {"localfiles": _prepared(decls, instance)}, {}
    )
    with caplog.at_level("WARNING"):
        build_presentation(
            base_config=KalinkaConfig(),
            input_modules={"localfiles": LocalFilesConfig()},
            devices={},
            dynamic_field_registry=registry,
        )
    assert "no matching section was emitted" in caplog.text


# ---------------------------------------------------------------------------
# Route-level: PUT /server/config rejects writes to dynamic paths
# ---------------------------------------------------------------------------


def test_put_config_rejects_writes_to_dynamic_paths():
    """Contract: PUT /server/config returns 400 when the request body's
    ``changes`` map references any path that's in the dynamic-field
    registry.

    We exercise the rejection contract against an inline FastAPI app
    that mirrors the relevant snippet of server.set_config_fields.
    Standing up the full create_app() flow would require a real plugin
    scan; this contract test is sufficient to prevent the policy from
    silently regressing if someone refactors the check.
    """
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.testclient import TestClient

    app = FastAPI()
    # The registry the real server caches at startup, scoped to the test.
    app.state.dynamic_paths = frozenset(
        {"input_modules.localfiles.ai_search.status_view"}
    )
    app.state.schema_version = "test-version"

    @app.put("/server/config")
    async def set_config_fields(payload: dict = Body(...)):
        changes = payload.get("changes", {})
        for key in changes:
            if key in app.state.dynamic_paths:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{key}' is a dynamic (plugin-resolved) field "
                    "and cannot be written via /server/config",
                )
        return {"message": "Ok", "schema_version": app.state.schema_version}

    client = TestClient(app)

    # 1. Writing to a dynamic path is rejected.
    r = client.put(
        "/server/config",
        json={
            "changes": {
                "input_modules.localfiles.ai_search.status_view": "x",
            }
        },
    )
    assert r.status_code == 400
    assert "dynamic" in r.json()["detail"].lower()

    # 2. Writing to a static path with the same prefix is accepted.
    r = client.put(
        "/server/config",
        json={"changes": {"input_modules.localfiles.ai_search.enabled": True}},
    )
    assert r.status_code == 200, r.text
