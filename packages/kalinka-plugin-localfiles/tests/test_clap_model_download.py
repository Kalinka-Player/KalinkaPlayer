"""Tests for the CLAP model-file downloader's partial-download safety.

The downloader writes to ``<dest>.part`` and atomic-renames on success.
Earlier revisions wrote directly to ``dest``, so an interrupted
download left a truncated file in place and the next call short-
circuited on ``os.path.isfile(dest)`` — onnxruntime then choked on
the partial file. These tests pin the rename pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kalinka_plugin_localfiles.embedder import clap_onnx


def _fake_urlretrieve_success(payload: bytes):
    """Return a urlretrieve stand-in that writes *payload* to the
    destination path the caller asks for."""

    def _fake(url, dest):
        Path(dest).write_bytes(payload)

    return _fake


def _fake_urlretrieve_failure(exc: Exception):
    """Return a urlretrieve stand-in that raises *exc* mid-download
    (after writing some bytes, to simulate a truncated download)."""

    def _fake(url, dest):
        # Write a partial blob first, then raise — mirrors what
        # urllib.request.urlretrieve does in real life when the
        # connection drops mid-stream.
        Path(dest).write_bytes(b"truncated-bytes")
        raise exc

    return _fake


def test_successful_download_writes_dest_and_removes_part(tmp_path, monkeypatch):
    payload = b"complete-onnx-bytes"
    monkeypatch.setattr(
        clap_onnx.urllib.request,
        "urlretrieve",
        _fake_urlretrieve_success(payload),
    )

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    expected = tmp_path / "clap_tokenizer.json"
    assert result == str(expected)
    assert expected.read_bytes() == payload
    # No stale .part residue from a successful run.
    assert not (tmp_path / "clap_tokenizer.json.part").exists()


def test_failed_download_does_not_install_partial_file(tmp_path, monkeypatch):
    """When urlretrieve raises, the destination must not exist — so a
    subsequent call sees os.path.isfile(dest) == False and retries the
    download instead of handing onnxruntime a truncated file."""
    monkeypatch.setattr(
        clap_onnx.urllib.request,
        "urlretrieve",
        _fake_urlretrieve_failure(RuntimeError("connection dropped")),
    )

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result is None
    dest = tmp_path / "clap_tokenizer.json"
    assert not dest.exists(), (
        "destination must not contain the partial blob after a failed download"
    )
    # The .part is also removed on the failure path (best-effort).
    assert not (tmp_path / "clap_tokenizer.json.part").exists()


def test_existing_dest_short_circuits(tmp_path, monkeypatch):
    """A complete file already on disk skips the download entirely."""
    dest = tmp_path / "clap_tokenizer.json"
    dest.write_bytes(b"already-here")

    call_count = {"n": 0}

    def _should_not_be_called(url, dst):
        call_count["n"] += 1

    monkeypatch.setattr(
        clap_onnx.urllib.request, "urlretrieve", _should_not_be_called
    )

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result == str(dest)
    assert call_count["n"] == 0


def test_stale_part_from_previous_run_is_cleaned_before_retry(
    tmp_path, monkeypatch
):
    """If a previous attempt was hard-killed (SIGKILL, power loss),
    the .part residue sits there. The next call must clear it before
    starting the new download — otherwise the new write would clobber
    only some of the bytes and we'd end up with a chimera."""
    stale_part = tmp_path / "clap_tokenizer.json.part"
    stale_part.write_bytes(b"stale-garbage")

    payload = b"fresh-bytes"
    monkeypatch.setattr(
        clap_onnx.urllib.request,
        "urlretrieve",
        _fake_urlretrieve_success(payload),
    )

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result == str(tmp_path / "clap_tokenizer.json")
    assert (tmp_path / "clap_tokenizer.json").read_bytes() == payload
    # The fake-urlretrieve writes the full payload to .part before the
    # rename, so the stale bytes must have been cleared first;
    # otherwise we'd see garbage prepended.
    assert not stale_part.exists()


def test_model_dir_is_created_before_use(tmp_path, monkeypatch):
    """Fresh hosts may not have /var/lib/kalinka/models when the
    embedder first runs. The downloader must create the directory
    tree up front so the rest of the function (download, manual
    copy via ckpt_path, etc.) can proceed."""
    nested = tmp_path / "var" / "lib" / "kalinka" / "models"
    assert not nested.exists()

    payload = b"x" * 16
    monkeypatch.setattr(
        clap_onnx.urllib.request,
        "urlretrieve",
        _fake_urlretrieve_success(payload),
    )

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(nested))

    assert nested.is_dir(), "nested model dir should be created on demand"
    assert result == str(nested / "clap_tokenizer.json")


def test_unknown_model_name_returns_none(tmp_path, monkeypatch):
    # No urlretrieve should be called.
    monkeypatch.setattr(
        clap_onnx.urllib.request,
        "urlretrieve",
        lambda *a, **kw: pytest.fail("urlretrieve should not be called"),
    )
    result = clap_onnx._ensure_model_file("does_not_exist", str(tmp_path))
    assert result is None


def test_models_download_from_the_project_organisation():
    """GitHub's rename redirect from the old owner ends once a repository is
    created again under that name."""
    for url in clap_onnx._MODEL_URLS.values():
        assert url.startswith(
            "https://github.com/Kalinka-Player/KalinkaPlayer/releases/download/"
        )
