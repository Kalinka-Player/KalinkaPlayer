"""The shared text embedder on a box without its runtime.

onnxruntime and tokenizers come from a plugin's optional packages, so a box
may not have them. The embedder must then say it is unavailable before it
downloads a model it could never run. With them, it fetches the model it
lacks, gives up on a download that stops coming, and tries a failed one
again only once it is due.
"""

import importlib.util
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kalinka_plugin_sdk.downloads import download_file
from kalinka_server import text_embedder
from kalinka_server.config_model import EmbeddingConfig, KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix
from kalinka_server.text_embedder import SharedTextEmbedder

RUNTIME = ("onnxruntime", "tokenizers")
MODEL_URL = "https://models.test/minilm"
MODEL_BODY = b"onnx" * 4096
# Stands in for the download's default timeout, which ensure_model keeps.
STALL_S = 0.5
# How long a stalled connection stays open: a download that waits for it
# has no timeout.
HOLD_S = 30
WITHIN_S = 10


@pytest.fixture
def served():
    """Serves MODEL_BODY at every path, keeping the paths asked for; with
    ``stall``, sends its first bytes and then nothing, the connection open."""
    answer = SimpleNamespace(stall=False, asked=[], url="")
    released = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            answer.asked.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(MODEL_BODY)))
            self.end_headers()
            if answer.stall:
                self.wfile.write(MODEL_BODY[:100])
                released.wait(HOLD_S)
            else:
                self.wfile.write(MODEL_BODY)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        answer.url = f"http://127.0.0.1:{server.server_port}/minilm"
        try:
            yield answer
        finally:
            released.set()
            server.shutdown()


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    fetched: list[str] = []

    def download_file(url: str, dest: str, *, max_bytes=None) -> bool:
        fetched.append(url)
        return False

    monkeypatch.setattr(text_embedder, "download_file", download_file)
    return fetched


def _installed(monkeypatch, names: set[str], loading: set[str] | None = None):
    """Make the runtime packages outside ``names`` unfindable and those in it
    findable, whatever this interpreter actually has; those in ``loading``,
    by default all of ``names``, import too."""
    find_spec = importlib.util.find_spec
    loading = names if loading is None else loading

    def fake_find_spec(name, *args, **kwargs):
        if name in RUNTIME:
            return object() if name in names else None
        return find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(text_embedder, "_loads", lambda name: name in loading)


def _probes(monkeypatch, loads: bool) -> list[str]:
    """The runtime packages whose import is tried, in order, each answering
    ``loads``."""
    probed: list[str] = []

    def probe(name: str) -> bool:
        probed.append(name)
        return loads

    monkeypatch.setattr(text_embedder, "_loads", probe)
    return probed


def _module(monkeypatch, tmp_path, name: str, body: str) -> None:
    """A module the probe's child process can import."""
    (tmp_path / f"{name}.py").write_text(body)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path), prepend=os.pathsep)


@pytest.mark.parametrize(
    "installed, missing",
    [
        (set(), ["onnxruntime", "tokenizers"]),
        ({"tokenizers"}, ["onnxruntime"]),
        ({"onnxruntime"}, ["tokenizers"]),
        (set(RUNTIME), []),
    ],
)
def test_it_names_the_runtime_it_cannot_import(
    monkeypatch, tmp_path, installed, missing
):
    """What a plugin that embeds offers to install."""
    _installed(monkeypatch, installed)
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    assert embedder.missing_runtime() == missing


def test_the_runtime_is_probed_once_however_often_it_is_asked(
    monkeypatch, tmp_path
):
    """A plugin asks from get_state(), which the modules page polls, and
    installing the runtime always restarts the server."""
    probed: list[str] = []
    find_spec = importlib.util.find_spec

    def counting_find_spec(name, *args, **kwargs):
        if name in RUNTIME:
            probed.append(name)
            return None
        return find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", counting_find_spec)
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    assert embedder.missing_runtime() == list(RUNTIME)
    assert embedder.missing_runtime() == list(RUNTIME)
    assert probed == list(RUNTIME)


def test_a_caller_cannot_change_the_answer_it_was_given(monkeypatch, tmp_path):
    _installed(monkeypatch, set())
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    embedder.missing_runtime().clear()

    assert embedder.missing_runtime() == list(RUNTIME)


@pytest.mark.parametrize("absent", RUNTIME)
async def test_without_its_runtime_it_is_unavailable_and_downloads_nothing(
    monkeypatch, tmp_path, downloads, absent
):
    _installed(monkeypatch, set(RUNTIME) - {absent})
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    assert await embedder.available() is False
    assert downloads == []
    assert not (tmp_path / "minilm").exists()


async def test_with_its_runtime_it_fetches_the_model_it_lacks(
    monkeypatch, tmp_path, downloads
):
    _installed(monkeypatch, set(RUNTIME))
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    assert await embedder.available() is False
    assert downloads == [f"{MODEL_URL}/model.onnx"]


async def test_with_its_runtime_the_model_lands_whole_from_its_url(
    monkeypatch, tmp_path, served
):
    _installed(monkeypatch, set(RUNTIME))
    model_dir = tmp_path / "minilm"
    embedder = SharedTextEmbedder(str(model_dir), served.url)

    assert await embedder.available() is True
    assert served.asked == ["/minilm/model.onnx", "/minilm/tokenizer.json"]
    assert sorted(p.name for p in model_dir.iterdir()) == [
        "model.onnx",
        "tokenizer.json",
    ]
    assert (model_dir / "model.onnx").read_bytes() == MODEL_BODY


async def test_a_model_download_that_stops_coming_is_given_up_leaving_nothing(
    monkeypatch, tmp_path, served
):
    """A connection that stalls without a reset, as on a Wi-Fi drop, would
    otherwise keep mood search being set up until the server restarts."""
    _installed(monkeypatch, set(RUNTIME))
    monkeypatch.setitem(download_file.__kwdefaults__, "timeout_s", STALL_S)
    served.stall = True
    model_dir = tmp_path / "minilm"
    embedder = SharedTextEmbedder(str(model_dir), served.url)

    started = time.monotonic()
    assert await embedder.available() is False

    assert time.monotonic() - started < WITHIN_S
    assert served.asked == ["/minilm/model.onnx"]
    assert list(model_dir.iterdir()) == []


async def test_a_failed_model_download_is_not_tried_again_before_it_is_due(
    monkeypatch, tmp_path, downloads
):
    """Every search during an outage would otherwise wait out a stall."""
    _installed(monkeypatch, set(RUNTIME))
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    assert await embedder.available() is False
    assert await embedder.available() is False
    assert downloads == [f"{MODEL_URL}/model.onnx"]


async def test_a_failed_model_download_is_tried_again_once_due(
    monkeypatch, tmp_path, served
):
    """Text search would otherwise stay off until the server restarts, the
    network long back; the runtime's import is not tried again."""
    _installed(monkeypatch, set(RUNTIME))
    probed = _probes(monkeypatch, loads=True)
    monkeypatch.setattr(text_embedder, "_RETRY_AFTER_S", 0.0)
    monkeypatch.setitem(download_file.__kwdefaults__, "timeout_s", STALL_S)
    served.stall = True
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), served.url)
    assert await embedder.available() is False

    served.stall = False

    assert await embedder.available() is True
    assert served.asked == [
        "/minilm/model.onnx",
        "/minilm/model.onnx",
        "/minilm/tokenizer.json",
    ]
    assert probed == list(RUNTIME)


async def test_a_runtime_that_does_not_load_is_not_tried_again(
    monkeypatch, tmp_path, downloads
):
    """Installing it restarts the server, so only a download is retried."""
    _installed(monkeypatch, set(RUNTIME))
    probed = _probes(monkeypatch, loads=False)
    monkeypatch.setattr(text_embedder, "_RETRY_AFTER_S", 0.0)
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    assert await embedder.available() is False
    assert await embedder.available() is False
    assert probed == list(RUNTIME)
    assert downloads == []


async def test_a_model_over_its_ceiling_is_refused_leaving_nothing(
    monkeypatch, tmp_path, served
):
    """A body without end would otherwise fill the card."""
    _installed(monkeypatch, set(RUNTIME))
    monkeypatch.setattr(text_embedder, "_MODEL_MAX_BYTES", len(MODEL_BODY) - 1)
    model_dir = tmp_path / "minilm"
    embedder = SharedTextEmbedder(str(model_dir), served.url)

    assert await embedder.available() is False

    assert served.asked == ["/minilm/model.onnx"]
    assert list(model_dir.iterdir()) == []


@pytest.mark.parametrize("broken", RUNTIME)
async def test_a_runtime_found_but_not_loading_is_missing_before_any_download(
    monkeypatch, tmp_path, downloads, broken
):
    """A missing shared library shows only on import, which would otherwise
    come after the model's download, on the first search."""
    _installed(monkeypatch, set(RUNTIME), loading=set(RUNTIME) - {broken})
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)
    assert embedder.missing_runtime() == []

    assert await embedder.available() is False
    assert downloads == []
    assert embedder.missing_runtime() == [broken]


def test_a_module_that_imports_loads():
    assert text_embedder._loads("json") is True


def test_a_module_whose_import_fails_does_not_load(monkeypatch, tmp_path, caplog):
    _module(
        monkeypatch,
        tmp_path,
        "broken_runtime",
        'raise ImportError("libgomp.so.1: cannot open shared object file")\n',
    )

    with caplog.at_level("WARNING"):
        assert text_embedder._loads("broken_runtime") is False
    assert "libgomp.so.1" in caplog.text


def test_a_module_killed_by_an_illegal_instruction_does_not_load(
    monkeypatch, tmp_path
):
    """Imported here, it would take the server down at every start."""
    _module(
        monkeypatch,
        tmp_path,
        "sigill_runtime",
        "import os, resource, signal\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "os.kill(os.getpid(), signal.SIGILL)\n",
    )

    assert text_embedder._loads("sigill_runtime") is False


async def test_embedding_without_the_runtime_is_refused(
    monkeypatch, tmp_path, downloads
):
    _installed(monkeypatch, set())
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    with pytest.raises(RuntimeError, match="unavailable"):
        await embedder.embed(["quiet piano"])


def test_the_model_downloads_from_the_project_organisation():
    """GitHub's rename redirect from the old owner ends once a repository is
    created again under that name."""
    assert EmbeddingConfig().model_url.startswith(
        "https://github.com/Kalinka-Player/KalinkaPlayer/releases/download/"
    )


@pytest.mark.parametrize(
    "saved, loaded",
    [
        (
            "https://github.com/madenvel/KalinkaPlayer/releases/download/jamendo-ai-v1",
            EmbeddingConfig().model_url,
        ),
        (MODEL_URL, MODEL_URL),
    ],
    ids=["former-owner", "own-url"],
)
def test_a_saved_model_url_follows_the_repository_move(saved, loaded):
    """A setting saved before the move would otherwise keep loading the model
    from whoever registers the old name."""
    config = KalinkaConfig()

    apply_overrides_with_prefix(
        config, {"base_config.embedding.model_url": saved}, "base_config."
    )

    assert config.embedding.model_url == loaded
