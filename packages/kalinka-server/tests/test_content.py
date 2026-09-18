"""The content endpoint and the URLs pointing at it.

The renderer is the demanding client here: it fetches in bounded ranges, reads
the stream size out of ``Content-Range`` on the first of them, and refuses to
seek at all unless ``Accept-Ranges`` says it may. These are the guarantees the
old in-plugin media server made and this endpoint inherits.

A module offers its content either as a file the server may read or as a
stream only the module can open — a share it speaks to itself. The guarantees
belong to the renderer, not to either arrangement, so the tests below run
against both.
"""

import io
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kalinka_plugin_sdk.inputmodule import ContentInfo, SourceUnavailableError

from kalinka_server.content_route import register_content_route
from kalinka_server.content_urls import CONTENT_ROUTE, content_url

AUDIO = bytes(range(256)) * 8  # 2048 bytes


class _FakeModule:
    def __init__(self, assets: dict[str, ContentInfo]):
        self._assets = assets

    async def get_content_info(self, asset_id):
        if asset_id == "offline":
            raise SourceUnavailableError("Music folder /mnt/nas is not available")
        if asset_id == "overran":
            # What TimeLimitedInputModule raises once the per-call budget is
            # spent; the module never gets to say why for itself.
            raise TimeoutError("localfiles.get_content_info exceeded the 3s budget")
        return self._assets.get(asset_id)


def _file_backed(path):
    return ContentInfo(mime_type="audio/flac", local_path=str(path), cacheable=True)


def _reader_backed(reader=None, size=len(AUDIO)):
    return ContentInfo(
        mime_type="audio/flac",
        reader=reader or (lambda: io.BytesIO(AUDIO)),
        size=size,
        cacheable=True,
    )


def _client_for(playable, extra=None):
    assets = {
        "track_1": playable,
        # A module may know the asset and still refuse to serve it.
        "unservable": ContentInfo(mime_type="audio/flac"),
        # Asset ids are the module's to mint and need not be one path
        # segment; this one survives only if the route allows a slash.
        "disc 1/track 1": playable,
    }
    assets.update(extra or {})
    modules = {"localfiles": _FakeModule(assets)}

    def resolve_module(name):
        if name not in modules:
            raise HTTPException(status_code=404, detail="Input module not found")
        return modules[name]

    app = FastAPI()
    register_content_route(app, resolve_module)
    return TestClient(app)


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "song.flac"
    path.write_bytes(AUDIO)
    return path


@pytest.fixture(params=["file", "reader"])
def client(request, audio_file):
    """The endpoint over both arrangements a module can offer."""
    playable = (
        _file_backed(audio_file) if request.param == "file" else _reader_backed()
    )
    return _client_for(playable)


@pytest.fixture
def file_client(audio_file):
    """Only for what is specific to a file the server reads itself."""
    return _client_for(_file_backed(audio_file))


@pytest.fixture
def stream_client():
    """Only for what is specific to a stream the server proxies."""
    return _client_for(_reader_backed())


def _url(module="localfiles", asset="track_1"):
    return f"{CONTENT_ROUTE}/{module}/{asset}"


def test_serves_the_whole_asset(client):
    r = client.get(_url())
    assert r.status_code == 200
    assert r.content == AUDIO
    assert r.headers["content-type"] == "audio/flac"


def test_advertises_range_support(client):
    """Without this the renderer refuses every seek."""
    assert client.get(_url()).headers["accept-ranges"] == "bytes"


def test_bounded_range_reports_the_total_size(client):
    """The renderer's first request is a bounded range, and it learns the
    stream size from nothing but this header."""
    r = client.get(_url(), headers={"Range": "bytes=0-511"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-511/{len(AUDIO)}"
    assert r.content == AUDIO[:512]


def test_open_ended_range_serves_to_the_end(client):
    r = client.get(_url(), headers={"Range": "bytes=2000-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 2000-2047/{len(AUDIO)}"
    assert r.content == AUDIO[2000:]


def test_suffix_range_serves_the_tail(client):
    r = client.get(_url(), headers={"Range": "bytes=-48"})
    assert r.status_code == 206
    assert r.content == AUDIO[-48:]


def test_unsatisfiable_range_is_refused(client):
    r = client.get(_url(), headers={"Range": f"bytes={len(AUDIO) + 10}-"})
    assert r.status_code == 416


def test_head_answers_without_a_body(client):
    r = client.head(_url())
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(AUDIO))
    assert r.content == b""


# The two paths part company on requests no renderer makes: FileResponse
# answers these itself, rejecting a unit it does not know and serving several
# ranges as a multipart reply. Proxied streams do neither — a range that
# cannot be honoured is ignored and the whole asset is served, which is what
# the HTTP specification allows and all a renderer would ever need.


def test_a_range_in_units_we_do_not_speak_is_ignored(stream_client):
    """Answering 206 would claim a range was honoured when none was."""
    r = stream_client.get(_url(), headers={"Range": "furlongs=0-1"})
    assert r.status_code == 200
    assert r.content == AUDIO


def test_several_ranges_at_once_are_answered_whole(stream_client):
    r = stream_client.get(_url(), headers={"Range": "bytes=0-99, 200-299"})
    assert r.status_code == 200
    assert r.content == AUDIO


def test_a_malformed_range_is_ignored(stream_client):
    r = stream_client.get(_url(), headers={"Range": "bytes=abc-def"})
    assert r.status_code == 200
    assert r.content == AUDIO


def test_a_range_that_ends_before_it_starts_is_ignored(stream_client):
    """An invalid header, which the specification has the server ignore —
    416 is for a well-formed span the asset cannot meet, and a client that
    mis-forms one still gets the track."""
    r = stream_client.get(_url(), headers={"Range": "bytes=100-50"})
    assert r.status_code == 200
    assert r.content == AUDIO


def test_head_reports_the_range_a_get_would_serve(client):
    """HEAD must not say one thing where GET says another."""
    r = client.head(_url(), headers={"Range": "bytes=0-511"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-511/{len(AUDIO)}"
    assert r.content == b""


def test_unknown_asset_is_absent(client):
    assert client.get(_url(asset="no_such_track")).status_code == 404


def test_asset_the_module_will_not_serve_is_absent(client):
    """A module reporting no servable file is a 404, never a 500."""
    assert client.get(_url(asset="unservable")).status_code == 404


def test_unknown_module_is_absent(client):
    assert client.get(_url(module="no_such_module")).status_code == 404


def test_transiently_unreachable_storage_is_a_503(client):
    """An unmounted share must not read as a missing file: renderers retry
    5xx but abort on 4xx, so 503 is what keeps the stream alive."""
    r = client.get(_url(asset="offline"))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


def test_a_module_cut_off_by_its_budget_is_a_503(client):
    """A module reaching storage that went quiet is cancelled by the per-call
    budget before it can report the outage itself. What escaped was a bare
    TimeoutError, which the framework served as a 500 — an unhandled error,
    on the one path that had promised a retryable answer."""
    r = client.get(_url(asset="overran"))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


def test_a_file_that_went_away_is_absent(file_client, audio_file):
    """Between the module's answer and the read — a 404, not a 500."""
    audio_file.unlink()
    assert file_client.get(_url()).status_code == 404


def test_a_hung_mount_does_not_pin_the_request(file_client, monkeypatch):
    """The last existence check before FileResponse is bounded too: a mount
    that hangs after the module answered reads as transient, and does not
    hold the event loop while it does."""
    import kalinka_server.content_route as content_route

    class _HungPath(Path):
        def is_file(self):
            time.sleep(5)
            return True

    monkeypatch.setattr(content_route, "_STAT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(content_route, "Path", _HungPath)

    r = file_client.get(_url())
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


def test_a_stream_without_a_length_is_absent():
    """A renderer reads the stream size out of Content-Range and cannot seek
    without it, so an unmeasured stream has nothing servable to offer."""
    client = _client_for(_reader_backed(size=None))
    assert client.get(_url()).status_code == 404


def test_a_stream_is_closed_once_the_response_ends():
    """One reader per request, and the server owns closing it: a share would
    otherwise accumulate an open handle per track played."""
    streams = []

    def open_stream():
        stream = io.BytesIO(AUDIO)
        streams.append(stream)
        return stream

    client = _client_for(_reader_backed(reader=open_stream))
    assert client.get(_url(), headers={"Range": "bytes=0-99"}).status_code == 206

    assert len(streams) == 1
    # Closing is handed to a worker thread, so it lands just after the reply.
    deadline = time.monotonic() + 5.0
    while not streams[0].closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert streams[0].closed


def test_a_stream_the_storage_refuses_is_transient():
    """The module measured the asset a moment ago, so a refusal now is the
    storage failing rather than the asset being gone — and a renderer
    abandons a track on 4xx while it retries 5xx. A dropped share session is
    worth retrying."""
    def refuse():
        raise OSError("the share went away")

    client = _client_for(_reader_backed(reader=refuse))
    r = client.get(_url())
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


def test_a_stream_whose_file_vanished_is_absent():
    """The one OSError that does mean absent, and it keeps its 404 so a
    deleted track is not retried forever."""
    def gone():
        raise FileNotFoundError("no such file")

    client = _client_for(_reader_backed(reader=gone))
    assert client.get(_url()).status_code == 404


def test_a_stream_that_will_not_open_is_transient(monkeypatch):
    """Opening is bounded like the file stat is: a share that stops answering
    reads as transient rather than as a missing track."""
    import kalinka_server.content_route as content_route

    def hang():
        time.sleep(5)
        return io.BytesIO(AUDIO)

    monkeypatch.setattr(content_route, "_OPEN_TIMEOUT_S", 0.05)
    client = _client_for(_reader_backed(reader=hang))

    r = client.get(_url())
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


def test_a_slow_open_is_not_held_to_the_stat_budget(monkeypatch):
    """Opening a stream may have to connect and log in first, which takes
    longer than a stat is ever given. Sharing one budget failed a healthy
    share whose pooled session had dropped."""
    import kalinka_server.content_route as content_route

    def unhurried():
        time.sleep(0.2)
        return io.BytesIO(AUDIO)

    monkeypatch.setattr(content_route, "_STAT_TIMEOUT_S", 0.05)
    client = _client_for(_reader_backed(reader=unhurried))

    r = client.get(_url())
    assert r.status_code == 200
    assert r.content == AUDIO


def test_content_url_is_built_on_the_address_the_fetcher_reached():
    assert (
        content_url(("192.168.1.10", 8000), "localfiles", "track_1")
        == "http://192.168.1.10:8000/content/localfiles/track_1"
    )


def test_content_url_brackets_an_ipv6_host():
    assert content_url(("fe80::1", 8000), "localfiles", "t1").startswith(
        "http://[fe80::1]:8000/"
    )


def test_a_minted_link_reaches_the_asset_it_names(client):
    """The link is the contract between content_url and this route: an id with
    a slash in it is escaped on the way out and must survive the round trip,
    which a single-segment route silently fails."""
    url = content_url(("testserver", 80), "localfiles", "disc 1/track 1")

    r = client.get(url.removeprefix("http://testserver:80"))

    assert r.status_code == 200
    assert r.content == AUDIO


def test_content_url_escapes_the_asset_id():
    """Ids are opaque — a module may mint one holding a slash or a space."""
    url = content_url(("10.0.0.1", 8000), "localfiles", "a b/c")
    assert url == "http://10.0.0.1:8000/content/localfiles/a%20b%2Fc"


def test_a_disabled_module_serves_no_content(monkeypatch):
    """A module the user has switched off must read as absent, not as a fault:
    `input_module` alone answers 500 once the interface has been torn down."""
    from kalinka_server import server

    prepared = {"localfiles": SimpleNamespace(interface=None)}
    monkeypatch.setattr(
        server.modules,
        "prepared_input_modules",
        prepared,
        raising=False,
    )
    monkeypatch.setattr(server.modules, "enabled_input_modules", set(), raising=False)

    with pytest.raises(HTTPException) as raised:
        server.enabled_input_module("localfiles")

    assert raised.value.status_code == 404
