"""Mood search's runtime is offered as optional packages.

The server does not install onnxruntime or tokenizers, so the plugin offers
them, reports what the shared embedder says it lacks, and ships the
allow-list the install helper checks requests against.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kalinka_plugin_jamendo import module_setup, optional_packages
from kalinka_plugin_jamendo.config_model import JamendoConfig
from kalinka_plugin_jamendo.module_setup import KalinkaPluginJamendo
from kalinka_plugin_sdk import ModuleHealthState, optional_packages_manifest

RUNTIME = ("onnxruntime", "tokenizers")
SDK_SRC = Path(__file__).resolve().parents[2] / "kalinka-plugin-sdk" / "src"


def _on(monkeypatch, architecture: str) -> None:
    monkeypatch.setattr(module_setup, "wheel_architecture", lambda: architecture)


@pytest.fixture(autouse=True)
def on_a_64_bit_pi(monkeypatch):
    _on(monkeypatch, "aarch64")


class FakeEmbedder:
    """Names the runtime it lacks, as the server's shared embedder does."""

    def __init__(self, missing=()):
        self._missing = list(missing)

    def missing_runtime(self) -> list[str]:
        return list(self._missing)


class OlderEmbedder:
    """The embedder of a server before SDK 3.12, which installed the runtime
    with itself and has no missing_runtime()."""


class UnprovisionedIndex:
    """Stands in for the mood index whose provisioning setup() starts."""

    def __init__(self, *args):
        pass

    async def available(self) -> bool:
        return False


def _plugin(embedder=None, **config) -> KalinkaPluginJamendo:
    plugin = KalinkaPluginJamendo()
    plugin._context = SimpleNamespace(
        config=JamendoConfig(**config),
        embedder=FakeEmbedder() if embedder is None else embedder,
    )
    return plugin


@pytest.fixture
async def set_up(monkeypatch):
    """Runs the plugin's own setup() on a context whose config the test
    keeps, so it can stage changes as PUT /server/config does."""
    monkeypatch.setattr(module_setup, "JamendoMoodIndex", UnprovisionedIndex)
    plugins: list[KalinkaPluginJamendo] = []

    async def set_up(embedder, config: JamendoConfig) -> KalinkaPluginJamendo:
        plugin = KalinkaPluginJamendo()
        await plugin.setup(SimpleNamespace(config=config, embedder=embedder))
        plugins.append(plugin)
        return plugin

    yield set_up
    for plugin in plugins:
        await plugin.shutdown()


def test_the_plugin_offers_mood_searchs_runtime():
    assert set(KalinkaPluginJamendo.OPTIONAL_PACKAGES) == set(RUNTIME)


def test_the_package_build_prints_the_plugins_allow_list():
    """As build_deb.sh and build_rpm.sh run it: a script beside the plugin's
    modules, with nothing but the SDK on the path."""
    result = subprocess.run(
        [sys.executable, optional_packages.__file__],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SDK_SRC)},
        check=True,
    )

    assert json.loads(result.stdout) == optional_packages_manifest(
        KalinkaPluginJamendo.PLUGIN_ID, KalinkaPluginJamendo.OPTIONAL_PACKAGES
    )


async def test_mood_search_without_onnxruntime_reports_it_missing():
    plugin = _plugin(
        FakeEmbedder(["onnxruntime"]), client_id="x", ai_search_enabled=True
    )

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == ["onnxruntime"]
    assert "`onnxruntime`" in state.message
    assert await plugin.required_packages() == ["onnxruntime"]


async def test_mood_search_without_its_runtime_reports_both():
    plugin = _plugin(FakeEmbedder(RUNTIME), client_id="x", ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == ["onnxruntime", "tokenizers"]
    assert await plugin.required_packages() == ["onnxruntime", "tokenizers"]


async def test_with_mood_search_off_nothing_is_missing():
    plugin = _plugin(FakeEmbedder(RUNTIME), client_id="x", ai_search_enabled=False)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.READY
    assert state.missing_packages == []
    assert await plugin.required_packages() == []


async def test_with_its_runtime_mood_search_stays_ready_and_unmentioned(set_up):
    plugin = await set_up(
        FakeEmbedder(), JamendoConfig(client_id="x", ai_search_enabled=True)
    )

    first = await plugin.get_state()
    second = await plugin.get_state()

    assert first.state == second.state == ModuleHealthState.READY
    assert first.missing_packages == second.missing_packages == []
    assert first.message is None and second.message is None
    assert await plugin.required_packages() == []


async def test_mood_search_turned_on_since_setup_reports_its_runtime_missing(
    set_up,
):
    """required_packages() must see config staged since setup, though the
    embedder probes its runtime only once."""
    config = JamendoConfig(client_id="x", ai_search_enabled=False)
    plugin = await set_up(FakeEmbedder(RUNTIME), config)
    assert (await plugin.get_state()).state == ModuleHealthState.READY

    config.ai_search_enabled = True
    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == ["onnxruntime", "tokenizers"]
    assert await plugin.required_packages() == ["onnxruntime", "tokenizers"]


async def test_mood_search_turned_off_since_setup_needs_nothing(set_up):
    config = JamendoConfig(client_id="x", ai_search_enabled=True)
    plugin = await set_up(FakeEmbedder(RUNTIME), config)
    assert (await plugin.get_state()).state == ModuleHealthState.WARNING

    config.ai_search_enabled = False

    assert (await plugin.get_state()).state == ModuleHealthState.READY
    assert await plugin.required_packages() == []


async def test_without_a_client_id_the_runtime_is_not_installed():
    """Nothing on Jamendo works before the client_id is set, so a box that
    never set Jamendo up never fetches mood search's runtime."""
    plugin = _plugin(FakeEmbedder(RUNTIME), ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.ERROR
    assert state.missing_packages == []
    assert await plugin.required_packages() == []


async def test_on_32_bit_arm_mood_search_is_unavailable_not_one_install_away(
    monkeypatch,
):
    """onnxruntime has no armv7l wheel, so an install there would only fail,
    on every restart."""
    _on(monkeypatch, "armv7l")
    plugin = _plugin(FakeEmbedder(RUNTIME), client_id="x", ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == []
    assert "not available on this device" in state.message
    assert "`onnxruntime`" in state.message
    assert "armv7l" in state.message
    assert "**Mood / AI search** off" in state.message
    assert await plugin.required_packages() == []


async def test_on_32_bit_arm_a_package_with_wheels_there_is_still_offered(
    monkeypatch,
):
    _on(monkeypatch, "armv7l")
    plugin = _plugin(
        FakeEmbedder(["tokenizers"]), client_id="x", ai_search_enabled=True
    )

    state = await plugin.get_state()

    assert state.missing_packages == ["tokenizers"]
    assert await plugin.required_packages() == ["tokenizers"]


async def test_a_runtime_package_jamendo_does_not_offer_leaves_it_unavailable():
    """A newer server's embedder may run on a package this plugin predates;
    the box could install it, so its architecture is not the reason."""
    plugin = _plugin(
        FakeEmbedder(["tokenizers", "sentencepiece"]),
        client_id="x",
        ai_search_enabled=True,
    )

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.WARNING
    assert state.missing_packages == []
    assert "`sentencepiece`" in state.message
    assert "Update the plugin" in state.message
    assert "aarch64" not in state.message
    assert await plugin.required_packages() == []


async def test_on_32_bit_arm_a_package_without_wheels_outranks_one_not_offered(
    monkeypatch,
):
    """Updating the plugin would not bring onnxruntime to this box."""
    _on(monkeypatch, "armv7l")
    plugin = _plugin(
        FakeEmbedder(["onnxruntime", "sentencepiece"]),
        client_id="x",
        ai_search_enabled=True,
    )

    state = await plugin.get_state()

    assert "not available on this device" in state.message
    assert "`onnxruntime`" in state.message
    assert await plugin.required_packages() == []


async def test_on_32_bit_arm_with_mood_search_off_nothing_is_said(monkeypatch):
    _on(monkeypatch, "armv7l")
    plugin = _plugin(FakeEmbedder(RUNTIME), client_id="x", ai_search_enabled=False)

    assert (await plugin.get_state()).state == ModuleHealthState.READY


async def test_an_older_servers_embedder_lacks_nothing():
    plugin = _plugin(OlderEmbedder(), client_id="x", ai_search_enabled=True)

    state = await plugin.get_state()

    assert state.state == ModuleHealthState.READY
    assert await plugin.required_packages() == []


async def test_a_plugin_never_set_up_needs_nothing():
    assert await KalinkaPluginJamendo().required_packages() == []
