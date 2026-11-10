"""Dropping a CLAP session gives its memory back to the OS, not just to glibc,
which otherwise keeps the freed heap and leaves the embedder at its loaded size."""

import platform
from unittest.mock import Mock

import pytest

from kalinka_plugin_localfiles import worker_utils
from kalinka_plugin_localfiles.embedder import clap_onnx
from kalinka_plugin_localfiles.embedder.clap_onnx import ClapOnnxModel


def test_trims_the_heap(monkeypatch):
    trim = Mock()
    monkeypatch.setattr(worker_utils, "_malloc_trim", lambda: trim)

    worker_utils.release_freed_memory()

    trim.assert_called_once_with(0)


def test_a_libc_without_malloc_trim_only_collects(monkeypatch):
    monkeypatch.setattr(worker_utils, "_malloc_trim", lambda: None)

    worker_utils.release_freed_memory()


@pytest.mark.skipif(platform.libc_ver()[0] != "glibc", reason="glibc only")
def test_glibc_offers_malloc_trim():
    assert worker_utils._malloc_trim() is not None
    worker_utils.release_freed_memory()


@pytest.fixture
def release(monkeypatch):
    release = Mock()
    monkeypatch.setattr(clap_onnx, "release_freed_memory", release)
    return release


def test_unloading_the_audio_tower_releases_its_memory(tmp_path, release):
    model = ClapOnnxModel(model_dir=str(tmp_path))
    model._audio_session = object()

    model.unload_audio()

    assert not model.is_audio_loaded
    release.assert_called_once_with()


def test_unloading_an_absent_audio_tower_does_nothing(tmp_path, release):
    ClapOnnxModel(model_dir=str(tmp_path)).unload_audio()

    release.assert_not_called()


def test_unloading_everything_releases_its_memory(tmp_path, release):
    model = ClapOnnxModel(model_dir=str(tmp_path))
    model._audio_session = object()
    model._text_session = object()

    model.unload()

    assert not model.is_audio_loaded and not model.is_text_loaded
    release.assert_called_once_with()
