"""Tests for the embedder's independent CLAP text/audio tower lifecycle.

The text tower serves search-query encoding and stays resident whenever AI
search is enabled; the audio tower is indexing-only and idles out after
``audio_model_idle_timeout_seconds``.
"""

from unittest.mock import Mock, patch

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb


class _FakeClap:
    """Stand-in for ClapOnnxModel that tracks per-tower load/unload."""

    def __init__(self, *_, **__):
        self._model_dir = "/fake/models"
        self.text_loaded = False
        self.audio_loaded = False

    def load_text(self):
        self.text_loaded = True

    def load_audio(self):
        self.audio_loaded = True

    def unload_audio(self):
        self.audio_loaded = False

    @property
    def is_text_loaded(self):
        return self.text_loaded

    @property
    def is_audio_loaded(self):
        return self.audio_loaded

    @property
    def has_va_head(self):
        return True


def _make_worker() -> EmbeddingWorker:
    config = LocalFilesConfig()
    db = Mock(spec=AsyncEmbedderDb)
    worker = EmbeddingWorker(config, db)

    # Replace the wrapper factory with one that builds a _FakeClap, mirroring
    # the real _new_clap's "instantiate once, reuse" contract.
    def _fake_new_clap():
        if worker._clap is None:
            worker._clap = _FakeClap()
        return worker._clap

    worker._new_clap = _fake_new_clap
    return worker


def _patch_deps():
    return patch.multiple(
        "kalinka_plugin_localfiles.embedder.embedder",
        _ensure_numpy=lambda: True,
        _ensure_package=lambda _name: True,
    )


class TestTowerLoading:
    def test_text_loads_without_audio(self):
        worker = _make_worker()
        with _patch_deps():
            assert worker._ensure_text_model() is True
        assert worker._text_available is True
        assert worker._audio_available is False
        assert worker._clap.audio_loaded is False

    def test_audio_loads_on_demand(self):
        worker = _make_worker()
        with _patch_deps():
            assert worker._ensure_audio_model() is True
        assert worker._audio_available is True
        # Audio load alone must not pull in the text tower.
        assert worker._text_available is False

    def test_both_towers_share_one_wrapper(self):
        worker = _make_worker()
        with _patch_deps():
            worker._ensure_text_model()
            worker._ensure_audio_model()
        assert worker._clap.text_loaded is True
        assert worker._clap.audio_loaded is True


class TestAudioIdleUnload:
    def test_unloads_after_idle_timeout(self):
        worker = _make_worker()
        with _patch_deps():
            worker._ensure_audio_model()
        assert worker._audio_available is True

        # Pretend the last embed was well beyond the timeout.
        worker._audio_last_used_at -= 1000
        worker._maybe_unload_audio(idle_timeout=900)

        assert worker._audio_available is False
        assert worker._clap.audio_loaded is False
        # Retry gate reset so the next batch reloads immediately.
        assert worker._audio_load_attempted_at == float("-inf")

    def test_keeps_loaded_within_timeout(self):
        worker = _make_worker()
        with _patch_deps():
            worker._ensure_audio_model()

        worker._maybe_unload_audio(idle_timeout=900)
        assert worker._audio_available is True

    def test_zero_timeout_never_unloads(self):
        worker = _make_worker()
        with _patch_deps():
            worker._ensure_audio_model()
        worker._audio_last_used_at -= 100_000
        worker._maybe_unload_audio(idle_timeout=0)
        assert worker._audio_available is True

    def test_unload_keeps_text_resident(self):
        worker = _make_worker()
        with _patch_deps():
            worker._ensure_text_model()
            worker._ensure_audio_model()
        worker._audio_last_used_at -= 1000
        worker._maybe_unload_audio(idle_timeout=900)
        assert worker._audio_available is False
        assert worker._text_available is True
        assert worker._clap.text_loaded is True
