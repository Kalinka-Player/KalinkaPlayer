"""The CLAP model files are fetched once, whole, and a fetch that stops
coming is given up.

A file lands at its path only once complete. Earlier revisions wrote
straight to it, so an interrupted download left a truncated file that the
next call took as present — onnxruntime then choked on the partial file.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kalinka_plugin_localfiles.embedder import clap_onnx
from kalinka_plugin_sdk.downloads import download_file

PAYLOAD = b"complete-onnx-bytes" * 1000
# Stands in for the download's default timeout, which the models keep.
STALL_S = 0.5
# How long a stalled connection stays open: a download that waits for it
# has no timeout.
HOLD_S = 30
WITHIN_S = 10


@pytest.fixture
def served(monkeypatch):
    """Serves PAYLOAD as every model file, keeping the paths asked for; with
    ``stall``, sends its first bytes and then nothing, the connection open."""
    answer = SimpleNamespace(stall=False, asked=[])
    released = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            answer.asked.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            if answer.stall:
                self.wfile.write(PAYLOAD[:100])
                released.wait(HOLD_S)
            else:
                self.wfile.write(PAYLOAD)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        base = f"http://127.0.0.1:{server.server_port}"
        for name, filename in clap_onnx._MODEL_FILENAMES.items():
            monkeypatch.setitem(clap_onnx._MODEL_URLS, name, f"{base}/{filename}")
        try:
            yield answer
        finally:
            released.set()
            server.shutdown()


def test_a_model_file_lands_whole_at_its_path(tmp_path, served):
    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    expected = tmp_path / "clap_tokenizer.json"
    assert result == str(expected)
    assert expected.read_bytes() == PAYLOAD
    assert served.asked == ["/clap_tokenizer.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clap_tokenizer.json"]


def test_a_download_that_stops_coming_is_given_up_and_tried_again(
    tmp_path, served, monkeypatch
):
    """Nothing it began is left for the next call to take as the model, so
    that call fetches it again."""
    monkeypatch.setitem(download_file.__kwdefaults__, "timeout_s", STALL_S)
    served.stall = True

    started = time.monotonic()
    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result is None
    assert time.monotonic() - started < WITHIN_S
    assert list(tmp_path.iterdir()) == []

    served.stall = False
    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result == str(tmp_path / "clap_tokenizer.json")
    assert (tmp_path / "clap_tokenizer.json").read_bytes() == PAYLOAD
    assert len(served.asked) == 2


def test_a_model_file_over_its_ceiling_is_refused_leaving_nothing(
    tmp_path, served, monkeypatch
):
    """A body without end would otherwise fill the card."""
    monkeypatch.setattr(clap_onnx, "_MODEL_MAX_BYTES", len(PAYLOAD) - 1)

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result is None
    assert served.asked == ["/clap_tokenizer.json"]
    assert list(tmp_path.iterdir()) == []


def test_existing_dest_short_circuits(tmp_path, served):
    """A complete file already on disk skips the download entirely."""
    dest = tmp_path / "clap_tokenizer.json"
    dest.write_bytes(b"already-here")

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result == str(dest)
    assert served.asked == []


def test_stale_part_from_previous_run_is_replaced(tmp_path, served):
    """If a previous attempt was hard-killed (SIGKILL, power loss), the
    .part residue sits there. The new download must start it afresh, or
    the model would be a chimera of both."""
    stale_part = tmp_path / "clap_tokenizer.json.part"
    stale_part.write_bytes(b"stale-garbage" * 10000)

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(tmp_path))

    assert result == str(tmp_path / "clap_tokenizer.json")
    assert (tmp_path / "clap_tokenizer.json").read_bytes() == PAYLOAD
    assert not stale_part.exists()


def test_model_dir_is_created_before_use(tmp_path, served):
    """Fresh hosts may not have /var/lib/kalinka/models when the
    embedder first runs. The downloader must create the directory
    tree up front so the rest of the function (download, manual
    copy via ckpt_path, etc.) can proceed."""
    nested = tmp_path / "var" / "lib" / "kalinka" / "models"
    assert not nested.exists()

    result = clap_onnx._ensure_model_file("clap_tokenizer", str(nested))

    assert nested.is_dir(), "nested model dir should be created on demand"
    assert result == str(nested / "clap_tokenizer.json")


def test_unknown_model_name_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        clap_onnx,
        "download_file",
        lambda *a, **kw: pytest.fail("download_file should not be called"),
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
