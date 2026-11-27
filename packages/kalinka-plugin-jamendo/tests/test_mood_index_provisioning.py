"""The mood index is fetched only once the shared embedder can run.

A box without the embedder's runtime could never query the ~140 MB index,
so it must not download it. Once fetched, a download that stops coming or
announces more than an index could hold is given up.
"""

import asyncio
import sqlite3
import threading
import time

import pytest
from kalinka_server.config_overrides import apply_overrides_with_prefix

from kalinka_plugin_jamendo import mood_search
from kalinka_plugin_jamendo.config_model import JamendoConfig
from kalinka_plugin_jamendo.mood_search import JamendoMoodIndex
from kalinka_plugin_sdk import downloads as sdk_downloads

INDEX_URL = "https://index.test/jamendo_index.sqlite"
# Well under the conftest server's hold on a stalled connection.
WITHIN_S = 10


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


async def test_an_index_set_up_again_mid_download_waits_for_that_download(
    tmp_path, monkeypatch
):
    """Shutting the plugin down cancels its provisioning, but the thread
    fetching the index carries on; the next setup must not fetch it again
    over the same temp file."""
    fetches: list[str] = []
    release = threading.Event()

    def fetch_whole(url, dest, **kwargs):
        fetches.append(url)
        release.wait(5)
        con = sqlite3.connect(dest)
        con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.commit()
        con.close()
        return True

    fetch = mood_search.download_file
    first_in, second_in = threading.Event(), threading.Event()

    def download_file(url: str, dest: str) -> bool:
        (second_in if first_in.is_set() else first_in).set()
        return fetch(url, dest)

    monkeypatch.setattr(sdk_downloads, "download_file", fetch_whole)
    monkeypatch.setattr(mood_search, "download_file", download_file)
    path = str(tmp_path / "jamendo_index_v2.sqlite")

    def provisioning() -> asyncio.Task:
        index = JamendoMoodIndex(path, INDEX_URL, FakeEmbedder(available=True))
        return asyncio.create_task(index.available())

    shut_down = provisioning()
    assert await asyncio.to_thread(first_in.wait, 5)
    shut_down.cancel()
    set_up_again = provisioning()
    assert await asyncio.to_thread(second_in.wait, 5)
    release.set()

    assert await set_up_again is True
    assert fetches == [INDEX_URL]


def test_an_index_download_that_stops_coming_is_given_up_leaving_nothing(
    tmp_path, index_server, stall_timeout
):
    """A connection that stalls without a reset, as on a Wi-Fi drop, would
    otherwise hold every later setup waiting on this download."""
    index_server.body = b"x" * 4096
    index_server.stall = True

    started = time.monotonic()
    fetched = mood_search.download_file(
        index_server.url, str(tmp_path / "jamendo_index_v2.sqlite")
    )

    assert fetched is False
    assert time.monotonic() - started < WITHIN_S
    assert list(tmp_path.iterdir()) == []


def test_an_index_announced_over_its_ceiling_is_refused_before_its_body(
    tmp_path, index_server
):
    """Nothing is sent after the headers, so a download that read on would
    wait out its whole timeout."""
    index_server.length = mood_search._INDEX_MAX_BYTES + 1
    index_server.stall = True

    started = time.monotonic()
    fetched = mood_search.download_file(
        index_server.url, str(tmp_path / "jamendo_index_v2.sqlite")
    )

    assert fetched is False
    assert time.monotonic() - started < WITHIN_S
    assert list(tmp_path.iterdir()) == []


def test_the_index_downloads_from_the_project_organisation():
    """GitHub's rename redirect from the old owner ends once a repository is
    created again under that name."""
    assert JamendoConfig().ai_index_url.startswith(
        "https://github.com/Kalinka-Player/KalinkaPlayer/releases/download/"
    )


@pytest.mark.parametrize(
    "saved, loaded",
    [
        (
            "https://github.com/madenvel/KalinkaPlayer/releases/download/"
            "jamendo-ai-v1/jamendo_index_v2.sqlite",
            JamendoConfig().ai_index_url,
        ),
        (INDEX_URL, INDEX_URL),
    ],
    ids=["former-owner", "own-url"],
)
def test_a_saved_index_url_follows_the_repository_move(saved, loaded):
    """A setting saved before the move would otherwise keep loading the index
    from whoever registers the old name."""
    config = JamendoConfig()

    apply_overrides_with_prefix(
        config, {"input_modules.jamendo.ai_index_url": saved}, "input_modules.jamendo."
    )

    assert config.ai_index_url == loaded
