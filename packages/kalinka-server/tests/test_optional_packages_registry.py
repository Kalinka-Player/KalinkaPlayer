"""Tests for the optional-packages registry that backs /server/optional_packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from kalinka_plugin_sdk import OptionalPackageSpec
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import InputModulePlugin
from kalinka_server.optional_packages_registry import (
    build_catalog,
    build_registry,
    write_pending_installs,
)


@dataclass
class FakePluginClass:
    OPTIONAL_PACKAGES: dict[str, OptionalPackageSpec]


@dataclass
class FakePrepared:
    plugin_class: type


def _prepared(packages: dict[str, OptionalPackageSpec]) -> FakePrepared:
    return FakePrepared(plugin_class=FakePluginClass(OPTIONAL_PACKAGES=packages))


def _spec(name: str, import_name: str = "") -> OptionalPackageSpec:
    return OptionalPackageSpec(
        pip_spec=f"{name}==1.0",
        description=f"desc for {name}",
        import_name=import_name,
    )


def test_registry_collects_from_input_modules_and_devices():
    input_modules = {
        "localfiles": _prepared({"numpy": _spec("numpy")}),
    }
    devices = {
        "fancydac": _prepared({"alsa_extras": _spec("alsa_extras")}),
    }
    registry = build_registry(input_modules, devices)
    assert set(registry) == {"numpy", "alsa_extras"}
    assert registry["numpy"][0] == "localfiles"
    assert registry["alsa_extras"][0] == "fancydac"


def test_plugin_declaring_none_contributes_none():
    """``OPTIONAL_PACKAGES`` is declared on PluginBase with an empty default,
    so a plugin with only hard dependencies needs no entry — and the
    registry's defensive getattr still holds for plugins built against an
    SDK that predates the declaration."""

    class _NoOptionalDeps(InputModulePlugin):
        PLUGIN_ID = "thin"
        REQUIRES_SDK = ">=1.0,<2"
        CONFIG_MODEL = ModuleConfig

        async def setup(self, context):  # pragma: no cover — never called
            pass

    assert _NoOptionalDeps.OPTIONAL_PACKAGES == {}
    prepared = FakePrepared(plugin_class=_NoOptionalDeps)
    assert build_registry({"thin": prepared}, {}) == {}


def test_registry_first_declarer_wins_on_duplicate_keys(caplog):
    input_modules = {
        "alpha": _prepared({"shared": _spec("shared-v1")}),
        "beta": _prepared({"shared": _spec("shared-v2")}),
    }
    with caplog.at_level("WARNING"):
        registry = build_registry(input_modules, {})
    assert registry["shared"][0] == "alpha"
    assert "declared by both" in caplog.text


def test_registry_takes_a_key_two_plugins_share_on_the_same_terms_quietly(caplog):
    """Two plugins that run on the same runtime both offer it."""
    input_modules = {
        "alpha": _prepared({"onnxruntime": _spec("onnxruntime")}),
        "beta": _prepared({"onnxruntime": _spec("onnxruntime")}),
    }
    with caplog.at_level("WARNING"):
        registry = build_registry(input_modules, {})
    assert registry["onnxruntime"][0] == "alpha"
    assert caplog.text == ""


def test_registry_warns_when_a_shared_key_probes_another_import_name(caplog):
    """The first declarer's import_name decides what counts as installed."""
    input_modules = {
        "alpha": _prepared({"shared": _spec("shared")}),
        "beta": _prepared({"shared": _spec("shared", import_name="shared_core")}),
    }
    with caplog.at_level("WARNING"):
        registry = build_registry(input_modules, {})
    assert registry["shared"][1].import_name == ""
    assert "declared by both" in caplog.text


def test_registry_warns_when_a_shared_key_lists_other_architectures(caplog):
    """The first declarer's list decides where the package is offered."""
    input_modules = {
        "alpha": _prepared({"onnxruntime": _spec("onnxruntime")}),
        "beta": _prepared(
            {
                "onnxruntime": OptionalPackageSpec(
                    pip_spec="onnxruntime==1.0",
                    description="desc for onnxruntime",
                    architectures=("x86_64", "aarch64"),
                )
            }
        ),
    }
    with caplog.at_level("WARNING"):
        registry = build_registry(input_modules, {})
    assert registry["onnxruntime"][1].architectures == ()
    assert "declared by both" in caplog.text


def test_registry_skips_malformed_entries(caplog):
    input_modules = {
        "weird": _prepared({"bogus": "not a spec object"}),  # type: ignore[dict-item]
    }
    with caplog.at_level("WARNING"):
        registry = build_registry(input_modules, {})
    assert "bogus" not in registry
    assert "expected OptionalPackageSpec" in caplog.text


def test_catalog_reports_installed_via_import_name(tmp_path: Path):
    # 'sys' is always importable; 'definitely_not_a_module' isn't.
    input_modules = {
        "p": _prepared(
            {
                "sys-pkg": _spec("sys-pkg", import_name="sys"),
                "ghost": _spec("ghost", import_name="definitely_not_a_module"),
            }
        ),
    }
    catalog = build_catalog(
        input_modules,
        {},
        last_install_path=tmp_path / "last.json",
        pending_installs_path=tmp_path / "pending.json",
    )
    by_key = {p["key"]: p for p in catalog["packages"]}
    assert by_key["sys-pkg"]["installed"] is True
    assert by_key["ghost"]["installed"] is False


def test_catalog_names_every_plugin_that_offers_a_shared_package(tmp_path: Path):
    """A user of the second plugin alone still learns what it needs the
    package for."""
    input_modules = {
        "localfiles": _prepared(
            {"onnxruntime": _spec("onnxruntime"), "soxr": _spec("soxr")}
        ),
        "jamendo": _prepared(
            {
                "onnxruntime": OptionalPackageSpec(
                    pip_spec="onnxruntime==1.0", description="mood search"
                )
            }
        ),
    }
    catalog = build_catalog(
        input_modules,
        {},
        last_install_path=tmp_path / "last.json",
        pending_installs_path=tmp_path / "pending.json",
    )
    by_key = {p["key"]: p for p in catalog["packages"]}

    assert by_key["onnxruntime"]["plugin_id"] == "localfiles"
    assert by_key["onnxruntime"]["description"] == "desc for onnxruntime"
    assert by_key["onnxruntime"]["offered_by"] == [
        {"plugin_id": "localfiles", "description": "desc for onnxruntime"},
        {"plugin_id": "jamendo", "description": "mood search"},
    ]
    assert by_key["soxr"]["offered_by"] == [
        {"plugin_id": "localfiles", "description": "desc for soxr"}
    ]


def test_catalog_marks_pending_keys(tmp_path: Path):
    input_modules = {
        "p": _prepared({"a": _spec("a"), "b": _spec("b")}),
    }
    pending_path = tmp_path / "pending.json"
    pending_path.write_text(
        json.dumps({"schema": 1, "requested": ["a"], "requested_at": 0})
    )
    catalog = build_catalog(
        input_modules,
        {},
        last_install_path=tmp_path / "last.json",
        pending_installs_path=pending_path,
    )
    by_key = {p["key"]: p for p in catalog["packages"]}
    assert by_key["a"]["pending"] is True
    assert by_key["b"]["pending"] is False


def test_catalog_includes_last_install_audit(tmp_path: Path):
    input_modules = {"p": _prepared({"a": _spec("a")})}
    last_install_path = tmp_path / "last.json"
    last_install_path.write_text(json.dumps({"results": [{"key": "a", "status": "ok"}]}))
    catalog = build_catalog(
        input_modules,
        {},
        last_install_path=last_install_path,
        pending_installs_path=tmp_path / "pending.json",
    )
    assert catalog["last_install"] == {"results": [{"key": "a", "status": "ok"}]}


def test_catalog_tolerates_malformed_last_install(tmp_path: Path, caplog):
    input_modules = {"p": _prepared({"a": _spec("a")})}
    last_install_path = tmp_path / "last.json"
    last_install_path.write_text("not json")
    with caplog.at_level("WARNING"):
        catalog = build_catalog(
            input_modules,
            {},
            last_install_path=last_install_path,
            pending_installs_path=tmp_path / "pending.json",
        )
    assert catalog["last_install"] is None
    assert "Cannot read last install" in caplog.text


def test_write_pending_installs_validates_and_dedupes(tmp_path: Path):
    input_modules = {"p": _prepared({"numpy": _spec("numpy"), "scipy": _spec("scipy")})}
    pending = tmp_path / "pending.json"
    accepted, rejected = write_pending_installs(
        ["numpy", "scipy", "bogus", "numpy"],
        input_modules,
        {},
        pending_installs_path=pending,
    )
    assert accepted == ["numpy", "scipy"]
    assert rejected == ["bogus"]
    payload = json.loads(pending.read_text())
    assert payload["schema"] == 1
    assert payload["requested"] == ["numpy", "scipy"]
    assert "requested_at" in payload


def test_write_pending_installs_atomic_replace(tmp_path: Path):
    """The pending file must be replaced atomically (no half-written state)."""
    input_modules = {"p": _prepared({"numpy": _spec("numpy")})}
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"schema": 1, "requested": ["old"]}))
    write_pending_installs(
        ["numpy"], input_modules, {}, pending_installs_path=pending
    )
    payload = json.loads(pending.read_text())
    assert payload["requested"] == ["numpy"]
    # No leftover .tmp
    assert list(tmp_path.glob("pending.json.tmp")) == []
