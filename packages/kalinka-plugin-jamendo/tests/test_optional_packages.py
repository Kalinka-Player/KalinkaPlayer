"""Mood search's runtime is offered as optional packages.

The server does not install onnxruntime or tokenizers, so the plugin offers
them, reports them while they are missing, and ships the allow-list the
install helper checks requests against.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from kalinka_plugin_jamendo import optional_packages
from kalinka_plugin_jamendo.config_model import JamendoConfig
from kalinka_plugin_jamendo.module_setup import KalinkaPluginJamendo
from kalinka_plugin_sdk import ModuleHealthState

RUNTIME = ("onnxruntime", "tokenizers")
SDK_SRC = Path(__file__).resolve().parents[2] / "kalinka-plugin-sdk" / "src"


def _installed(monkeypatch, names: set[str]) -> None:
    """Make the runtime packages outside ``names`` unfindable and those in it
    findable, whatever this interpreter actually has."""
    find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name in RUNTIME:
            return object() if name in names else None
        return find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


def _plugin(**config) -> KalinkaPluginJamendo:
    plugin = KalinkaPluginJamendo()
    plugin._context = SimpleNamespace(config=JamendoConfig(**config))
    return plugin


def test_the_manifest_is_the_plugins_allow_list():
    manifest = optional_packages.manifest()

    assert manifest["plugin"] == KalinkaPluginJamendo.PLUGIN_ID
    assert manifest["packages"] == {
        key: spec.model_dump()
        for key, spec in KalinkaPluginJamendo.OPTIONAL_PACKAGES.items()
    }
    assert set(manifest["packages"]) == set(RUNTIME)


def test_the_package_build_prints_the_manifest():
    """As build_deb.sh and build_rpm.sh run it: a script beside the plugin's
    modules, with nothing but the SDK on the path."""
    result = subprocess.run(
        [sys.executable, optional_packages.__file__],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SDK_SRC)},
        check=True,
    )

    assert json.loads(result.stdout) == optional_packages.manifest()


async def test_mood_search_without_onnxruntime_reports_it_missing(monkeypatch):
    _installed(monkeypatch, {"tokenizers"})
    plugin = _plugin(client_id="x", ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == ["onnxruntime"]
    assert "`onnxruntime`" in state.message
    assert await plugin.required_packages() == ["onnxruntime"]


async def test_mood_search_without_its_runtime_reports_both(monkeypatch):
    _installed(monkeypatch, set())
    plugin = _plugin(client_id="x", ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == ["onnxruntime", "tokenizers"]
    assert await plugin.required_packages() == ["onnxruntime", "tokenizers"]


async def test_with_mood_search_off_nothing_is_missing(monkeypatch):
    _installed(monkeypatch, set())
    plugin = _plugin(client_id="x", ai_search_enabled=False)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.READY
    assert state.missing_packages == []
    assert await plugin.required_packages() == []


async def test_with_its_runtime_mood_search_is_ready(monkeypatch):
    _installed(monkeypatch, set(RUNTIME))
    plugin = _plugin(client_id="x", ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.READY
    assert state.missing_packages == []
    assert await plugin.required_packages() == []


async def test_without_a_client_id_the_runtime_is_not_installed(monkeypatch):
    """Nothing on Jamendo works before the client_id is set, so a box that
    never set Jamendo up never fetches mood search's runtime."""
    _installed(monkeypatch, set())
    plugin = _plugin(ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.ERROR
    assert state.missing_packages == []
    assert await plugin.required_packages() == []


async def test_a_plugin_never_set_up_needs_nothing():
    assert await KalinkaPluginJamendo().required_packages() == []
