#!/usr/bin/env python3
"""What reading a track on a share costs the NAS, by who is reading it.

Tag reads and the audio embedder seek about a file, and a 1 MiB read-ahead
saves them round trips. The server reads a renderer's range in chunks capped
at what is left of it, so under the stream it is handed a read-ahead only
fetches bytes that are thrown away: 2.7 times the audio played, as a NAS
audit measured. The storage also keeps the file open between ranges, so
only a track's first range waits for the share to be probed and the file
measured and opened. Everything here reads through the storage the configuration
builds, over an in-memory share that buffers as ``smbclient`` does.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from fastapi import FastAPI
from mutagen.flac import FLAC

import kalinka_plugin_localfiles.storage.smb as smb_mod
from kalinka_plugin_localfiles.config_model import (
    AccountSignIn,
    LocalFilesConfig,
    SmbLocation,
    SmbSource,
)
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles
from kalinka_plugin_localfiles.storage import build_resolver
from kalinka_server.content_route import register_content_route
from kalinka_server.content_urls import CONTENT_ROUTE
from smb_fakes import FakeSmbClient

TRACK = "smb://nas/music/01 noise.flac"
TRACK_UNC = r"\\nas\music\01 noise.flac"

#: What a renderer asks for at a time.
RANGE_BYTES = 384_000


@pytest.fixture(scope="module")
def noise_flac(tmp_path_factory) -> bytes:
    """Eight seconds of noise, which FLAC cannot shrink below three ranges."""
    scratch = tmp_path_factory.mktemp("noise") / "noise.flac"
    noise = np.random.default_rng(142).uniform(-1.0, 1.0, (8 * 44100, 2))
    sf.write(str(scratch), noise, 44100, format="FLAC")
    audio = FLAC(str(scratch))
    audio.update({"title": "Noise", "artist": "A", "album": "B"})
    audio.save()
    return scratch.read_bytes()


def _nas(password: str) -> SmbSource:
    return SmbSource(
        id="nas",
        location=SmbLocation(host="nas", path="music"),
        authentication=AccountSignIn(username="media", password=password),
    )


@pytest.fixture
def share(monkeypatch, tmp_path, noise_flac):
    client = FakeSmbClient(
        listings={r"\\nas\music": []},
        files={TRACK_UNC: noise_flac},
    )
    monkeypatch.setattr(smb_mod, "smbclient", client)
    # Refusals outlive a test; one left behind would refuse this logon.
    monkeypatch.setattr(smb_mod, "_REFUSALS", smb_mod._RefusedLogins())
    config = LocalFilesConfig(
        music_sources=[_nas(password="hunter2")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    return client, config


async def _index(config) -> str:
    await init_db(config.db_path)
    changes = await FileIndexer(config, AsyncIndexerDb(config)).process_file(TRACK)
    assert changes and changes["tracks"]
    return changes["tracks"]


def _open_bufferings(client) -> list[int]:
    return [kwargs["buffering"] for _, kwargs in client.calls if "buffering" in kwargs]


class _FragmentEncoder:
    """Stands in for CLAP, sampling a few fragments as the real one does."""

    def get_audio_embedding(self, audio):
        for offset in (0, 400_000, 800_000):
            audio.seek(offset)
            audio.read(65_536)
        return np.ones(512, dtype="float32")


@pytest.mark.asyncio
async def test_tags_are_read_with_read_ahead(share):
    client, config = share

    assert await _index(config)

    assert set(_open_bufferings(client)) == {smb_mod._READ_BUFFER}


def test_fragments_are_embedded_with_read_ahead(share):
    client, config = share
    worker = EmbeddingWorker(config, AsyncEmbedderDb(config))
    worker._clap = _FragmentEncoder()
    worker._audio_available = True

    assert worker._compute_clap_audio(TRACK) is not None

    assert _open_bufferings(client) == [smb_mod._READ_BUFFER]
    assert [opened.reads for opened in client.opened] == [1]


@pytest_asyncio.fixture
async def one_worker():
    """A single worker thread behind ``asyncio.to_thread``.

    The server gives a response's reader back on a worker nobody waits for.
    With one worker that is done before the next range is opened, as it
    nearly always is for a renderer, which sends each range once the last
    has arrived.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(executor)
    yield
    executor.shutdown(wait=True)


def _serving(module) -> httpx.AsyncClient:
    app = FastAPI()
    register_content_route(app, lambda _name: module)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kalinka"
    )


def _module_reading(config, resolver) -> LocalFilesInputModule:
    """The module as the plugin builds it: one resolver for every request."""
    return LocalFilesInputModule(
        config, LocalFilesInputModuleDb(config), storage_source=lambda: resolver
    )


def _ranged(start: int) -> dict[str, str]:
    return {"Range": f"bytes={start}-{start + RANGE_BYTES - 1}"}


def _trips(client) -> list[tuple[str, str]]:
    return [
        ("open" if "buffering" in kwargs else "stat", unc)
        for unc, kwargs in client.calls
    ]


@pytest.mark.asyncio
async def test_ranges_served_cost_the_nas_only_their_own_bytes(share, one_worker):
    """Three ranges in a row, as a renderer plays a track, through the
    server's content route. Each used to fetch a whole 1 MiB read-ahead for
    the 384,000 bytes it served, and to probe the share, measure the file and
    open it again before reading it in two."""
    client, config = share
    track_id = await _index(config)
    module = _module_reading(config, build_resolver(config))
    audio = client.files[TRACK_UNC]
    client.calls.clear()
    client.opened.clear()

    async with _serving(module) as http:
        for start in range(0, 3 * RANGE_BYTES, RANGE_BYTES):
            r = await http.get(
                f"{CONTENT_ROUTE}/localfiles/{track_id}", headers=_ranged(start)
            )
            assert r.status_code == 206
            assert r.headers["content-length"] == str(RANGE_BYTES)
            assert r.content == audio[start:start + RANGE_BYTES]

    assert _trips(client) == [
        ("stat", r"\\nas\music"),
        ("stat", TRACK_UNC),
        ("open", TRACK_UNC),
    ]
    assert _open_bufferings(client) == [0]
    [opened] = client.opened
    assert (opened.reads, opened.fetched) == (3, 3 * RANGE_BYTES)


@pytest.mark.asyncio
async def test_a_seek_while_the_track_streams_is_served_its_own_bytes(share):
    client, config = share
    track_id = await _index(config)
    module = _module_reading(config, build_resolver(config))
    audio = client.files[TRACK_UNC]
    url = f"{CONTENT_ROUTE}/localfiles/{track_id}"

    async with _serving(module) as http:
        whole, seek = await asyncio.gather(
            http.get(url), http.get(url, headers=_ranged(2 * RANGE_BYTES))
        )

    assert whole.content == audio
    assert seek.content == audio[2 * RANGE_BYTES:3 * RANGE_BYTES]


@pytest.mark.asyncio
async def test_closing_the_share_closes_the_track_it_kept_open(share, one_worker):
    """Before its connections go, while the session can still close it."""
    client, config = share
    track_id = await _index(config)
    resolver = build_resolver(config)
    client.opened.clear()

    async with _serving(_module_reading(config, resolver)) as http:
        await http.get(f"{CONTENT_ROUTE}/localfiles/{track_id}", headers=_ranged(0))
    # The single worker has handed the reader back once this has run.
    await asyncio.to_thread(lambda: None)
    [opened] = client.opened
    assert not opened.closed

    resolver.close()

    assert opened.closed
    assert client.resets == [[]]


@pytest.mark.asyncio
async def test_a_saved_password_closes_the_track_kept_open_as_the_old_one(
    share, one_worker
):
    client, config = share
    track_id = await _index(config)
    plugin = KalinkaPluginLocalFiles()
    plugin._context = SimpleNamespace(config=config)
    module = LocalFilesInputModule(
        config,
        LocalFilesInputModuleDb(config),
        storage_source=plugin._current_resolver,
    )
    client.opened.clear()

    async with _serving(module) as http:
        await http.get(f"{CONTENT_ROUTE}/localfiles/{track_id}", headers=_ranged(0))
    await asyncio.to_thread(lambda: None)
    [opened] = client.opened
    assert not opened.closed
    # Held here, so that collecting it cannot be what closes the file.
    replaced = plugin._current_resolver()

    config.music_sources = [_nas(password="hunter3")]
    config.reconcile(frozenset({"music_sources"}))

    assert plugin._current_resolver() is not replaced
    assert await _eventually(lambda: opened.closed)


async def _eventually(condition, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return condition()
