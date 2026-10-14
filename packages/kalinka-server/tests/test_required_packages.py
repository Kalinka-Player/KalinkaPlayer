"""Tests for plugin-side required_packages() auto-detection.

The server's PUT /server/restart unions the result of each plugin's
required_packages() with the optional explicit body and writes the
combined list to /var/lib/kalinka/pending_installs.json. This file
tests the plugin-side computation; the server merge is exercised
end-to-end via the write_pending_installs unit tests in
test_optional_packages_registry.py.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import patch

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.module_setup import (
    KalinkaPluginLocalFiles,
)


class _FakeContext:
    """Minimal stand-in for InputPluginContext — only `config` is read."""

    def __init__(self, config):
        self.config = config


def _plugin_with(config: LocalFilesConfig) -> KalinkaPluginLocalFiles:
    plugin = KalinkaPluginLocalFiles()
    plugin._context = _FakeContext(config)
    return plugin


def _set_importable(monkeypatch, available: set[str]) -> None:
    """Patch the plugin's _is_importable helper to return True only for
    names in *available*. Avoids actually probing the real Python env
    so tests don't depend on which optional ML packages happen to be
    installed locally."""
    from kalinka_plugin_localfiles import module_setup

    monkeypatch.setattr(
        module_setup, "_is_importable", lambda name: name in available
    )


def test_default_plugin_base_returns_empty():
    """The SDK default for plugins without optional deps is []."""
    from kalinka_plugin_sdk.plugin import PluginBase

    # Instantiate a bare PluginBase subclass to read the default impl.
    class _Stub(PluginBase):
        PLUGIN_ID = "stub"
        PLUGIN_TYPE = None  # type: ignore[assignment]
        CONFIG_MODEL = None  # type: ignore[assignment]

        async def setup(self, context):  # pragma: no cover — not called
            pass

    assert asyncio.run(_Stub().required_packages()) == []


def test_returns_empty_when_ai_search_is_off(monkeypatch):
    """AI search off → nothing to install."""
    cfg = LocalFilesConfig()
    cfg.ai_search.enabled = False
    _set_importable(monkeypatch, set())  # nothing installed

    plugin = _plugin_with(cfg)
    assert asyncio.run(plugin.required_packages()) == []


def test_returns_empty_when_ai_search_on_and_deps_present(monkeypatch):
    """No install is queued when the imports already resolve."""
    cfg = LocalFilesConfig()
    cfg.ai_search.enabled = True
    _set_importable(
        monkeypatch,
        {"numpy", "onnxruntime", "soundfile", "soxr", "tokenizers"},
    )

    plugin = _plugin_with(cfg)
    assert asyncio.run(plugin.required_packages()) == []


def test_ai_search_pulls_full_clap_stack(monkeypatch):
    """One flag covers indexing and querying, so it pulls both legs'
    dependencies — numpy for mood ranking plus the CLAP stack — and
    lists the shared numpy once."""
    cfg = LocalFilesConfig()
    cfg.ai_search.enabled = True
    _set_importable(monkeypatch, set())

    plugin = _plugin_with(cfg)
    result = asyncio.run(plugin.required_packages())
    assert result.count("numpy") == 1
    assert set(result) == {
        "numpy", "onnxruntime", "soundfile", "soxr", "tokenizers"
    }


def test_partial_install_only_lists_missing(monkeypatch):
    """Packages already importable are filtered out; only the missing
    ones come back."""
    cfg = LocalFilesConfig()
    cfg.ai_search.enabled = True
    # numpy + tokenizers installed; onnxruntime + soundfile + soxr are not.
    _set_importable(monkeypatch, {"numpy", "tokenizers"})

    plugin = _plugin_with(cfg)
    result = asyncio.run(plugin.required_packages())
    assert set(result) == {"onnxruntime", "soundfile", "soxr"}


def test_nothing_for_ai_search_where_onnxruntime_has_no_wheel(monkeypatch):
    """On 32-bit ARM the rest of the CLAP stack would be of no use."""
    from kalinka_plugin_localfiles import module_setup

    cfg = LocalFilesConfig()
    cfg.ai_search.enabled = True
    _set_importable(monkeypatch, set())
    monkeypatch.setattr(module_setup, "wheel_architecture", lambda: "armv7l")

    assert asyncio.run(_plugin_with(cfg).required_packages()) == []


def test_returns_empty_when_setup_never_ran():
    """Defensive: required_packages() is callable on a fresh instance
    that hasn't been set up yet, and returns [] rather than raising."""
    plugin = KalinkaPluginLocalFiles()
    assert plugin._context is None
    assert asyncio.run(plugin.required_packages()) == []
