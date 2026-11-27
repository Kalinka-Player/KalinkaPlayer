"""Shared test setup: an index served on this machine."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kalinka_plugin_sdk.downloads import download_file

STALL_S = 0.5
# How long a stalled connection stays open: a download that waits for it has
# no timeout.
HOLD_S = 30


@pytest.fixture
def index_server():
    """Serves ``body`` at every path under a Content-Length of ``length``, its
    own by default, keeping the paths asked for. With ``stall``, sends the
    first 100 bytes and then nothing, the connection left open."""
    served = SimpleNamespace(body=b"", length=None, stall=False, asked=[], url="")
    released = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            served.asked.append(self.path)
            length = len(served.body) if served.length is None else served.length
            self.send_response(200)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if served.stall:
                self.wfile.write(served.body[:100])
                released.wait(HOLD_S)
            else:
                self.wfile.write(served.body)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        served.url = f"http://127.0.0.1:{server.server_port}/jamendo_index_v2.sqlite"
        try:
            yield served
        finally:
            released.set()
            server.shutdown()


@pytest.fixture
def stall_timeout(monkeypatch) -> float:
    """Shortens the download's wait for the next byte, which the index's
    download leaves at its default."""
    monkeypatch.setitem(download_file.__kwdefaults__, "timeout_s", STALL_S)
    return STALL_S
