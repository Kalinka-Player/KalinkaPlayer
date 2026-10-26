"""The shared text embedder on a box without its runtime.

onnxruntime and tokenizers come from a plugin's optional packages, so a box
may not have them. The embedder must then say it is unavailable before it
downloads a model it could never run.
"""

import importlib.util
import os

import pytest

from kalinka_server import text_embedder
from kalinka_server.config_model import EmbeddingConfig
from kalinka_server.text_embedder import SharedTextEmbedder

RUNTIME = ("onnxruntime", "tokenizers")
MODEL_URL = "https://models.test/minilm"


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    fetched: list[str] = []

    def download_file(url: str, dest: str) -> bool:
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
