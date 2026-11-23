"""A download gives up on a connection that stops sending, and on a body
that is cut short or runs on, rather than hold its thread for ever or keep a
file that is not whole."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kalinka_plugin_sdk.downloads import download_file

# Over one read, so a stall can come after part of the body is on disk.
PAYLOAD = bytes(range(256)) * 6144
STALL_S = 0.5
# How long the server keeps a stalled connection open; a download that waits
# for it has no timeout.
HOLD_S = 30
WITHIN_S = 10


@pytest.fixture
def served():
    """Answers every request with ``status`` and ``body``, under a
    Content-Length of ``length`` (none when None), then, with ``hold``, keeps
    the connection open sending nothing more."""
    answer = SimpleNamespace(
        status=200, body=PAYLOAD, length=len(PAYLOAD), hold=False, url=""
    )
    released = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(answer.status)
            if answer.length is not None:
                self.send_header("Content-Length", str(answer.length))
            self.end_headers()
            self.wfile.write(answer.body)
            if answer.hold:
                released.wait(HOLD_S)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        answer.url = f"http://127.0.0.1:{server.server_port}/model.onnx"
        try:
            yield answer
        finally:
            released.set()
            server.shutdown()


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "models" / "model.onnx"


def _part(dest):
    return dest.with_name(dest.name + ".part")


def _timed(*args, **kwargs):
    started = time.monotonic()
    result = download_file(*args, **kwargs)
    return result, time.monotonic() - started


def test_a_whole_body_lands_at_dest(served, dest):
    assert download_file(served.url, str(dest)) is True

    assert dest.read_bytes() == PAYLOAD
    assert not _part(dest).exists()


def test_a_body_without_a_content_length_lands_whole(served, dest):
    served.length = None

    assert download_file(served.url, str(dest)) is True

    assert dest.read_bytes() == PAYLOAD


@pytest.mark.parametrize(
    "sent", [1000, len(PAYLOAD) - 1000], ids=["in-the-first-read", "in-a-later-read"]
)
def test_a_body_that_stops_coming_is_given_up_within_the_timeout(served, dest, sent):
    served.body = PAYLOAD[:sent]
    served.hold = True

    result, elapsed = _timed(served.url, str(dest), timeout_s=STALL_S)

    assert result is False
    assert elapsed < WITHIN_S
    assert not dest.exists()
    assert not _part(dest).exists()


def test_a_body_shorter_than_its_content_length_is_refused(served, dest):
    served.body = PAYLOAD[: len(PAYLOAD) // 2]

    assert download_file(served.url, str(dest)) is False

    assert not dest.exists()
    assert not _part(dest).exists()


def test_a_body_running_over_max_bytes_is_refused(served, dest):
    served.length = None

    assert download_file(served.url, str(dest), max_bytes=len(PAYLOAD) - 1) is False

    assert not dest.exists()
    assert not _part(dest).exists()


def test_a_length_announced_over_max_bytes_is_refused_before_the_body(served, dest):
    served.length = 10**12
    served.body = b""
    served.hold = True

    result, elapsed = _timed(
        served.url, str(dest), timeout_s=HOLD_S, max_bytes=10**9
    )

    assert result is False
    assert elapsed < WITHIN_S
    assert not dest.exists()
    assert not _part(dest).exists()


def test_a_body_of_exactly_max_bytes_lands(served, dest):
    assert download_file(served.url, str(dest), max_bytes=len(PAYLOAD)) is True

    assert dest.read_bytes() == PAYLOAD


def test_an_error_answer_is_a_failure(served, dest):
    served.status = 404
    served.body = b"Not Found"
    served.length = len(served.body)

    assert download_file(served.url, str(dest)) is False

    assert not dest.exists()
    assert not _part(dest).exists()


def test_a_part_left_by_a_killed_download_is_replaced(served, dest):
    dest.parent.mkdir()
    _part(dest).write_bytes(b"stale" * 1000000)

    assert download_file(served.url, str(dest)) is True

    assert dest.read_bytes() == PAYLOAD
    assert not _part(dest).exists()


def test_a_failed_download_leaves_dest_as_it_was(served, dest):
    """Only a whole body ever replaces it."""
    dest.parent.mkdir()
    dest.write_bytes(b"earlier")
    served.body = PAYLOAD[:1000]

    assert download_file(served.url, str(dest)) is False

    assert dest.read_bytes() == b"earlier"


def test_the_log_names_the_address_without_its_secrets(served, dest, caplog):
    """An asset's address may carry a key, which no log may hold."""
    host_and_path = served.url.removeprefix("http://")
    with caplog.at_level("INFO"):
        download_file(f"{served.url}?key=s3cret", str(dest))
        served.status = 500
        download_file(f"http://user:hunter2@{host_and_path}?key=s3cret", str(dest))

    assert caplog.text.count(served.url) >= 2
    assert "download failed" in caplog.text
    assert "s3cret" not in caplog.text
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize(
    "address",
    ["index.test/model.onnx?key=s3cret", "{url}?key=s3cret\nx"],
    ids=["no-scheme", "control-character"],
)
def test_an_error_quoting_the_address_logs_none_of_its_secrets(
    served, dest, caplog, address
):
    with caplog.at_level("INFO"):
        assert download_file(address.format(url=served.url), str(dest)) is False

    assert "download failed" in caplog.text
    assert "s3cret" not in caplog.text


def test_an_unparsable_address_is_a_failure(dest, caplog):
    with caplog.at_level("INFO"):
        assert download_file("http://[::1/model.onnx?key=s3cret", str(dest)) is False

    assert "s3cret" not in caplog.text
