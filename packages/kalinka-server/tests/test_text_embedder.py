"""The shared text embedder on a box without its runtime.

onnxruntime and tokenizers come from a plugin's optional packages, so a box
may not have them. The embedder must then say it is unavailable before it
downloads a model it could never run.
"""

import importlib.util

import pytest

from kalinka_server import text_embedder
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


def _installed(monkeypatch, names: set[str]) -> None:
    """Make the runtime packages outside ``names`` unfindable and those in it
    findable, whatever this interpreter actually has."""
    find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name in RUNTIME:
            return object() if name in names else None
        return find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


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


async def test_embedding_without_the_runtime_is_refused(
    monkeypatch, tmp_path, downloads
):
    _installed(monkeypatch, set())
    embedder = SharedTextEmbedder(str(tmp_path / "minilm"), MODEL_URL)

    with pytest.raises(RuntimeError, match="unavailable"):
        await embedder.embed(["quiet piano"])
