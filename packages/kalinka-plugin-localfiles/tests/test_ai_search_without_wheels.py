"""AI search on a box where its runtime has no wheel.

The server leaves such a package out of its catalog and refuses it in PUT
/server/restart, so My Library must not report it in ``missing_packages``,
which the app sends straight back in that request.
"""

import asyncio
from unittest.mock import Mock, patch

from kalinka_plugin_localfiles import module_setup
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_sdk.module_health import ModuleHealthState


async def _noop(*args, **kwargs):
    return None


async def _empty_roots(*args, **kwargs):
    return []


def _evaluated(tmp_path, monkeypatch, *, architecture, importable):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    config.ai_search.enabled = True
    monkeypatch.setattr(module_setup, "wheel_architecture", lambda: architecture)
    monkeypatch.setattr(module_setup, "_is_importable", lambda name: name in importable)
    plugin = module_setup.KalinkaPluginLocalFiles()
    plugin._librarian_proc = Mock(is_alive=Mock(return_value=True))
    with patch.object(module_setup.asyncio, "sleep", _noop):
        asyncio.run(plugin._evaluate_subfeatures(config))
    return plugin


def _module_state(plugin):
    with patch.object(plugin, "_music_folder_statuses", _empty_roots):
        return asyncio.run(plugin.get_state())


def test_32_bit_arm_offers_no_install(tmp_path, monkeypatch):
    plugin = _evaluated(tmp_path, monkeypatch, architecture="armv7l", importable=set())
    ai = plugin._subfeatures["ai_search"]
    assert ai.state is ModuleHealthState.ERROR
    assert ai.missing_packages == ()
    assert "`onnxruntime`" in ai.message and "armv7l" in ai.message
    assert "Restart with install" not in ai.message
    assert _module_state(plugin).missing_packages == []


def test_64_bit_arm_still_offers_the_install(tmp_path, monkeypatch):
    plugin = _evaluated(tmp_path, monkeypatch, architecture="aarch64", importable=set())
    assert set(_module_state(plugin).missing_packages) == {
        "numpy",
        "onnxruntime",
        "soundfile",
        "soxr",
        "tokenizers",
    }
