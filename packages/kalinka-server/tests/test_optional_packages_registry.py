"""Tests for the optional-packages registry that backs /server/optional_packages."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kalinka_plugin_sdk import (
    ModuleHealthState,
    ModuleState,
    OptionalPackageSpec,
    wheel_architecture,
)
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import InputModulePlugin
from kalinka_server import install_pending, optional_packages_registry
from kalinka_server.optional_packages_registry import (
    as_offered,
    build_catalog,
    build_registry,
    queue_required_packages,
    unavailable_packages,
    write_pending_installs,
)

SIXTY_FOUR_BIT_ONLY = OptionalPackageSpec(
    pip_spec="onnxruntime==1.0",
    description="desc for onnxruntime",
    architectures=("x86_64", "aarch64"),
)


def _on(monkeypatch, architecture: str) -> None:
    monkeypatch.setattr(
        optional_packages_registry, "wheel_architecture", lambda: architecture
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


def test_catalog_leaves_out_a_package_with_no_wheel_for_this_box(
    monkeypatch, tmp_path: Path
):
    """An older app would offer to install whatever the catalog lists."""
    _on(monkeypatch, "armv7l")
    input_modules = {
        "localfiles": _prepared(
            {"onnxruntime": SIXTY_FOUR_BIT_ONLY, "soxr": _spec("soxr")}
        ),
    }

    catalog = build_catalog(
        input_modules,
        {},
        last_install_path=tmp_path / "last.json",
        pending_installs_path=tmp_path / "pending.json",
    )

    assert [p["key"] for p in catalog["packages"]] == ["soxr"]


def test_catalog_lists_a_package_where_it_has_wheels(monkeypatch, tmp_path: Path):
    _on(monkeypatch, "aarch64")
    input_modules = {"localfiles": _prepared({"onnxruntime": SIXTY_FOUR_BIT_ONLY})}

    catalog = build_catalog(
        input_modules,
        {},
        last_install_path=tmp_path / "last.json",
        pending_installs_path=tmp_path / "pending.json",
    )

    assert [p["key"] for p in catalog["packages"]] == ["onnxruntime"]


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


def test_write_pending_installs_refuses_a_package_with_no_wheel_for_this_box(
    monkeypatch, tmp_path: Path
):
    """What My Library's required_packages() asks for on 32-bit Pi OS: the
    install could only fail, again at every restart."""
    _on(monkeypatch, "armv7l")
    input_modules = {
        "localfiles": _prepared(
            {"onnxruntime": SIXTY_FOUR_BIT_ONLY, "soxr": _spec("soxr")}
        ),
    }
    pending = tmp_path / "pending.json"

    accepted, rejected = write_pending_installs(
        ["onnxruntime", "soxr"], input_modules, {}, pending_installs_path=pending
    )

    assert accepted == ["soxr"]
    assert rejected == ["onnxruntime"]
    assert json.loads(pending.read_text())["requested"] == ["soxr"]


def test_write_pending_installs_takes_a_package_where_it_has_wheels(
    monkeypatch, tmp_path: Path
):
    _on(monkeypatch, "x86_64")
    input_modules = {"localfiles": _prepared({"onnxruntime": SIXTY_FOUR_BIT_ONLY})}

    accepted, rejected = write_pending_installs(
        ["onnxruntime"],
        input_modules,
        {},
        pending_installs_path=tmp_path / "pending.json",
    )

    assert (accepted, rejected) == (["onnxruntime"], [])


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


def test_write_pending_installs_with_nothing_accepted_removes_the_request(
    tmp_path: Path,
):
    """install_pending would record a run of nothing over the last install."""
    input_modules = {"p": _prepared({"numpy": _spec("numpy")})}
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"schema": 1, "requested": ["numpy"]}))

    accepted, rejected = write_pending_installs(
        ["bogus"], input_modules, {}, pending_installs_path=pending
    )

    assert (accepted, rejected) == ([], ["bogus"])
    assert not pending.exists()


class _Needing:
    """A plugin whose required_packages() answers what the test says."""

    def __init__(self, keys: list[str] | None = None, answer: Any = None):
        self.keys = keys or []
        self.answer = answer

    async def required_packages(self) -> list[str]:
        if self.answer is not None:
            return await self.answer()
        return self.keys


@dataclass
class _Running:
    plugin_class: Any
    plugin_instance: Any
    plugin_context: Any = field(
        default_factory=lambda: SimpleNamespace(config=SimpleNamespace(enabled=True))
    )


def _running(offered: list[str], needs: list[str] = (), **kwargs) -> _Running:
    return _Running(
        plugin_class=FakePluginClass(
            OPTIONAL_PACKAGES={key: _spec(key) for key in offered}
        ),
        plugin_instance=_Needing(list(needs), **kwargs),
    )


def _queue(tmp_path: Path, input_modules, devices=None, requested=()):
    return asyncio.run(
        queue_required_packages(
            input_modules,
            devices or {},
            requested,
            pending_installs_path=tmp_path / "pending.json",
            last_install_path=tmp_path / "last.json",
        )
    )


def _queued(tmp_path: Path) -> list[str] | None:
    pending = tmp_path / "pending.json"
    return json.loads(pending.read_text())["requested"] if pending.exists() else None


def test_queue_takes_what_was_asked_for_then_what_plugins_need_in_load_order(
    tmp_path: Path,
):
    input_modules = {
        "alpha": _running(["x", "y", "v"], ["x", "y"]),
        "beta": _running(["z"], ["y", "z"]),
    }
    devices = {"amp": _running(["w"], ["w"])}

    accepted, rejected = _queue(tmp_path, input_modules, devices, ["z", "v"])

    assert (accepted, rejected) == (["z", "v", "x", "y", "w"], [])
    assert _queued(tmp_path) == ["z", "v", "x", "y", "w"]


def test_queue_writes_what_bootstrap_reads(tmp_path: Path):
    _queue(tmp_path, {"alpha": _running(["x"], ["x"])})

    payload = json.loads((tmp_path / "pending.json").read_text())
    assert set(payload) == {"schema", "requested_at", "requested"}
    assert payload["schema"] == 1
    assert payload["requested"] == ["x"]


def test_queue_takes_a_feature_turned_off_back_out(tmp_path: Path):
    plugin = _running(["x"], ["x"])
    _queue(tmp_path, {"alpha": plugin})
    plugin.plugin_instance.keys = []

    assert _queue(tmp_path, {"alpha": plugin}) == ([], [])
    assert _queued(tmp_path) is None


def test_queue_asks_no_disabled_plugin(tmp_path: Path):
    plugin = _running(["x"], ["x"])
    plugin.plugin_context.config.enabled = False

    assert _queue(tmp_path, {"alpha": plugin}) == ([], [])


def test_queue_drops_an_unknown_plugin_key_and_says_so(tmp_path: Path, caplog):
    with caplog.at_level("WARNING"):
        accepted, rejected = _queue(tmp_path, {"alpha": _running(["x"], ["x", "q"])})

    assert (accepted, rejected) == (["x"], ["q"])
    assert "Not queueing ['q']" in caplog.text


def test_queue_warns_of_a_conflicting_declaration_once(tmp_path: Path, caplog):
    """Each save queues, so each save should log the conflict only once."""
    input_modules = {
        "alpha": _running(["shared"], ["shared"]),
        "beta": _Running(
            plugin_class=FakePluginClass(
                OPTIONAL_PACKAGES={"shared": _spec("shared-v2")}
            ),
            plugin_instance=_Needing(),
        ),
    }

    with caplog.at_level("WARNING"):
        accepted, _rejected = _queue(tmp_path, input_modules)

    assert accepted == ["shared"]
    assert caplog.text.count("declared by both") == 1


async def _raises():
    raise RuntimeError("401 for https://api.test/?client_id=s3cret")


async def _hangs():
    await asyncio.Event().wait()


def test_queue_skips_a_plugin_that_raises_or_hangs(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr(optional_packages_registry, "PLUGIN_CALL_TIMEOUT_S", 0.01)
    input_modules = {
        "raising": _running(["r"], answer=_raises),
        "hanging": _running(["h"], answer=_hangs),
        "alpha": _running(["x"], ["x"]),
    }

    with caplog.at_level("WARNING"):
        accepted, _rejected = _queue(tmp_path, input_modules)

    assert accepted == ["x"]
    assert "raising required_packages() failed (RuntimeError)" in caplog.text
    assert "hanging required_packages() failed (TimeoutError)" in caplog.text
    assert "s3cret" not in caplog.text


def _here() -> str:
    return (
        f"for Python {sys.version_info.major}.{sys.version_info.minor} "
        f"on {wheel_architecture()}"
    )


def _last_install(tmp_path: Path, key: str, reason: str, pip_spec: str = "") -> None:
    (tmp_path / "last.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "requested": [key],
                "results": [
                    {
                        "key": key,
                        "pip_spec": pip_spec or f"{key}==1.0",
                        "returncode": 1,
                        "status": "failed",
                        "stderr": "",
                        "reason": reason,
                    }
                ],
            }
        )
    )


def test_queue_drops_a_plugin_key_the_last_install_found_no_wheel_of(
    tmp_path: Path, caplog
):
    """#332: otherwise every save and restart queues an install that can
    only fail again."""
    _last_install(tmp_path, "soxr", f"No prebuilt soxr==1.0 {_here()}")

    with caplog.at_level("WARNING"):
        accepted, rejected = _queue(
            tmp_path, {"alpha": _running(["soxr", "x"], ["soxr", "x"])}
        )

    assert (accepted, rejected) == (["x"], ["soxr"])
    assert _queued(tmp_path) == ["x"]
    assert "Not queueing ['soxr']" in caplog.text


def test_queue_takes_a_key_asked_for_though_the_last_install_found_no_wheel(
    tmp_path: Path,
):
    """An index may carry the wheel by now, so a retry stays possible."""
    _last_install(tmp_path, "soxr", f"No prebuilt soxr==1.0 {_here()}")

    accepted, rejected = _queue(
        tmp_path, {"alpha": _running(["soxr"], ["soxr"])}, requested=["soxr"]
    )

    assert (accepted, rejected) == (["soxr"], [])


@pytest.mark.parametrize(
    "reason, pip_spec",
    [
        pytest.param("No prebuilt soxr==1.0 for Python 3.0 on {arch}", "", id="python"),
        pytest.param("No prebuilt soxr==1.0 for Python {python} on vax", "", id="arch"),
        pytest.param("No prebuilt soxr==0.9 {here}", "soxr==0.9", id="older-spec"),
    ],
)
def test_a_no_wheel_failure_under_other_terms_does_not_stop_the_queue(
    tmp_path: Path, reason, pip_spec
):
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    _last_install(
        tmp_path,
        "soxr",
        reason.format(arch=wheel_architecture(), python=python, here=_here()),
        pip_spec,
    )

    accepted, _rejected = _queue(tmp_path, {"alpha": _running(["soxr"], ["soxr"])})

    assert accepted == ["soxr"]


def test_queue_reads_install_pendings_own_no_wheel_reason(
    tmp_path: Path, monkeypatch
):
    """The reason's wording is install_pending's; the queue only matches it."""
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "alpha.json").write_text(
        json.dumps(
            {"plugin": "alpha", "packages": {"soxr": {"pip_spec": "soxr==1.0"}}}
        )
    )
    page = "https://index.test/simple/soxr/"
    monkeypatch.setattr(
        install_pending,
        "_pip_install",
        lambda spec: install_pending.PipRun(
            1,
            f"ERROR: No matching distribution found for {spec}",
            f"Getting page {page}\nFetched page {page} as text/html\n",
        ),
    )
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"schema": 1, "requested": ["soxr"]}))
    install_pending.main(
        ["install_pending", str(pending), str(manifests), str(tmp_path / "last.json")]
    )

    accepted, rejected = _queue(tmp_path, {"alpha": _running(["soxr"], ["soxr"])})

    assert (accepted, rejected) == ([], ["soxr"])


def test_unavailable_names_declared_and_found_packages_without_wheels(
    monkeypatch, tmp_path: Path
):
    _on(monkeypatch, "armv7l")
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    found = f"No prebuilt soxr==1.0 for Python {python} on armv7l"
    _last_install(tmp_path, "soxr", found)
    input_modules = {
        "localfiles": _prepared(
            {
                "onnxruntime": SIXTY_FOUR_BIT_ONLY,
                "soxr": _spec("soxr"),
                "numpy": _spec("numpy"),
            }
        ),
    }

    unavailable = unavailable_packages(
        input_modules, {}, last_install_path=tmp_path / "last.json"
    )

    assert unavailable == {
        "onnxruntime": "No prebuilt onnxruntime for armv7l",
        "soxr": found,
    }


def test_a_module_offers_no_package_this_box_cannot_install():
    state = ModuleState(
        state=ModuleHealthState.ERROR,
        message="AI search unavailable.",
        missing_packages=["numpy", "soxr"],
    )

    shown = as_offered(state, {"soxr": "No prebuilt soxr for armv7l"})

    assert shown.missing_packages == ["numpy"]
    assert shown.message == (
        "AI search unavailable.\n\n"
        "`soxr` is not available on this device. No prebuilt soxr for armv7l."
    )
    assert state.missing_packages == ["numpy", "soxr"]


def test_a_module_offering_only_installable_packages_is_shown_as_it_said():
    state = ModuleState(
        state=ModuleHealthState.WARNING, missing_packages=["numpy"]
    )

    assert as_offered(state, {"soxr": "No prebuilt soxr for armv7l"}) is state
