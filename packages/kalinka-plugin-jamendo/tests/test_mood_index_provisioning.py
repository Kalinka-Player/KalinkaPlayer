"""The mood index is fetched only once the shared embedder can run.

A box without the embedder's runtime could never query the ~140 MB index,
so it must not download it.
"""

import pytest

from kalinka_plugin_jamendo import mood_search
from kalinka_plugin_jamendo.config_model import JamendoConfig
from kalinka_plugin_jamendo.mood_search import JamendoMoodIndex

INDEX_URL = "https://index.test/jamendo_index.sqlite"


class FakeEmbedder:
    model_id = "all-MiniLM-L6-v2"
    model_version = 1
    dim = 384

    def __init__(self, available: bool):
        self._available = available
        self.asked = False

    async def available(self) -> bool:
        self.asked = True
        return self._available


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    fetched: list[str] = []

    def download_file(url: str, dest: str) -> bool:
        fetched.append(url)
        return False

    monkeypatch.setattr(mood_search, "download_file", download_file)
    return fetched


async def test_with_the_embedder_unavailable_the_index_is_not_fetched(
    tmp_path, downloads
):
    index = JamendoMoodIndex(
        str(tmp_path / "index.sqlite"), INDEX_URL, FakeEmbedder(available=False)
    )

    assert await index.available() is False
    assert downloads == []


async def test_with_the_embedder_available_a_missing_index_is_fetched(
    tmp_path, downloads
):
    index = JamendoMoodIndex(
        str(tmp_path / "index.sqlite"), INDEX_URL, FakeEmbedder(available=True)
    )

    assert await index.available() is False
    assert downloads == [INDEX_URL]


async def test_a_failed_download_is_logged_as_one(tmp_path, downloads, caplog):
    """The index had a URL to come from, so it is not reported missing."""
    index = JamendoMoodIndex(
        str(tmp_path / "index.sqlite"), INDEX_URL, FakeEmbedder(available=True)
    )

    with caplog.at_level("WARNING"):
        assert await index.available() is False

    assert "could not fetch the Jamendo index" in caplog.text
    assert "index missing" not in caplog.text


async def test_without_an_index_or_its_url_the_embedder_is_not_provisioned(
    tmp_path, downloads
):
    """Provisioning the embedder downloads its ~90 MB model, which a box
    with no index to match against could never use."""
    embedder = FakeEmbedder(available=True)
    index = JamendoMoodIndex(str(tmp_path / "index.sqlite"), None, embedder)

    assert await index.available() is False
    assert embedder.asked is False
    assert downloads == []


async def test_an_embedder_of_another_model_fetches_nothing(tmp_path, downloads):
    embedder = FakeEmbedder(available=True)
    embedder.model_version = 2
    index = JamendoMoodIndex(str(tmp_path / "index.sqlite"), INDEX_URL, embedder)

    assert await index.available() is False
    assert downloads == []


def test_the_index_downloads_from_the_project_organisation():
    """GitHub's rename redirect from the old owner ends once a repository is
    created again under that name."""
    assert JamendoConfig().ai_index_url.startswith(
        "https://github.com/Kalinka-Player/KalinkaPlayer/releases/download/"
    )
