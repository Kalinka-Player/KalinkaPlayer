#!/usr/bin/env python3
"""What reading a track on a share costs the NAS, by who is reading it.

Tag reads and the audio embedder seek about a file, and a 1 MiB read-ahead
saves them round trips. The server reads a renderer's range in chunks capped
at what is left of it, so under the stream it is handed a read-ahead only
fetches bytes that are thrown away: 2.7 times the audio played, as a NAS
audit measured. Everything here reads through the storage the configuration
builds, over an in-memory share that buffers as ``smbclient`` does.
"""

import httpx
import numpy as np
import pytest
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
from kalinka_server.content_route import register_content_route
from kalinka_server.content_urls import CONTENT_ROUTE
from smb_fakes import FakeSmbClient

TRACK = "smb://nas/music/01 noise.flac"
TRACK_UNC = r"\\nas\music\01 noise.flac"

#: What a renderer asks for at a time.
RANGE_BYTES = 384_000


def _noise_flac(tmp_path) -> bytes:
    """Eight seconds of noise, which FLAC cannot shrink below three ranges."""
    scratch = tmp_path / "noise.flac"
    noise = np.random.default_rng(142).uniform(-1.0, 1.0, (8 * 44100, 2))
    sf.write(str(scratch), noise, 44100, format="FLAC")
    audio = FLAC(str(scratch))
    audio.update({"title": "Noise", "artist": "A", "album": "B"})
    audio.save()
    return scratch.read_bytes()


@pytest.fixture
def share(monkeypatch, tmp_path):
    client = FakeSmbClient(
        listings={r"\\nas\music": []},
        files={TRACK_UNC: _noise_flac(tmp_path)},
    )
    monkeypatch.setattr(smb_mod, "smbclient", client)
    config = LocalFilesConfig(
        music_sources=[
            SmbSource(
                id="nas",
                location=SmbLocation(host="nas", path="music"),
                authentication=AccountSignIn(username="media", password="hunter2"),
            )
        ],
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


@pytest.mark.asyncio
async def test_ranges_served_cost_the_nas_only_their_own_bytes(share):
    """Three ranges in a row, as a renderer plays a track, through the
    server's content route. Each used to fetch a whole 1 MiB read-ahead for
    the 384,000 bytes it served."""
    client, config = share
    track_id = await _index(config)
    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    app = FastAPI()
    register_content_route(app, lambda _name: module)
    audio = client.files[TRACK_UNC]
    client.calls.clear()
    client.opened.clear()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kalinka"
    ) as http:
        for start in range(0, 3 * RANGE_BYTES, RANGE_BYTES):
            r = await http.get(
                f"{CONTENT_ROUTE}/localfiles/{track_id}",
                headers={"Range": f"bytes={start}-{start + RANGE_BYTES - 1}"},
            )
            assert r.status_code == 206
            assert r.headers["content-length"] == str(RANGE_BYTES)
            assert r.content == audio[start:start + RANGE_BYTES]

    assert [opened.fetched for opened in client.opened] == [RANGE_BYTES] * 3
    assert _open_bufferings(client) == [0, 0, 0]
