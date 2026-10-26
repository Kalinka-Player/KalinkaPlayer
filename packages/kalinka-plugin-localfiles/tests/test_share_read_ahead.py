#!/usr/bin/env python3
"""What reading a track on a share costs the NAS, by who is reading it.

Tag reads and the audio embedder seek about a file, and a read-ahead saves
them round trips, though each place they seek to costs a whole one: 1 MiB
suits the embedder's audio fragments, while a tag reader looks for a few KB
at either end of a file. The server reads a renderer's range in chunks
capped at what is left of it, so under the stream it is handed a read-ahead
only fetches bytes that are thrown away: 2.7 times the audio played, as a
NAS audit measured. The storage also keeps the file open between ranges, so
only a track's first range waits for the share to be probed and the file
measured and opened. Everything here reads through the storage the configuration
builds, over an in-memory share that buffers as ``smbclient`` does.
"""

import asyncio
import io
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import httpx
import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from fastapi import FastAPI
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TIT2, TPE1
from mutagen.id3 import delete as delete_id3
from mutagen.mp3 import MP3
from smbprotocol.exceptions import SMBConnectionClosed

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
from kalinka_plugin_localfiles.indexer.indexer import (
    TAG_READ_AHEAD_BYTES,
    FileIndexer,
)
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
MP3_TRACK = "smb://nas/music/02 ladder.mp3"
MP3_TRACK_UNC = r"\\nas\music\02 ladder.mp3"
LADDER_MP3 = (
    Path(__file__).resolve().parents[2]
    / "kalinka-renderer"
    / "tests"
    / "data"
    / "ladder.mp3"
)
#: A picture several times the tag read-ahead.
COVER = bytes(range(256)) * 1200

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


@pytest.fixture(scope="module")
def mp3_frames(tmp_path_factory) -> bytes:
    """The renderer's ladder untagged and 64 times over: 3 MB of frames,
    which put a file's two ends far apart."""
    scratch = tmp_path_factory.mktemp("ladder") / "ladder.mp3"
    shutil.copyfile(LADDER_MP3, scratch)
    delete_id3(scratch)
    return scratch.read_bytes() * 64


def _tagged_mp3(tmp_path, frames: bytes, cover: Optional[bytes] = None) -> bytes:
    """``frames`` under a few KB of ID3 tags, with ``cover`` among them."""
    scratch = tmp_path / "tagged.mp3"
    scratch.write_bytes(frames)
    tags = ID3()
    tags.add(TIT2(text="Ladder"))
    tags.add(TPE1(text="The Sines"))
    tags.add(TALB(text="Test Tones"))
    tags.add(COMM(lang="eng", desc="", text="liner notes " * 250))
    if cover is not None:
        tags.add(APIC(mime="image/png", type=3, desc="", data=cover))
    tags.save(scratch)
    return scratch.read_bytes()


def _covered_flac(tmp_path, flac: bytes, cover: bytes) -> bytes:
    scratch = tmp_path / "covered.flac"
    scratch.write_bytes(flac)
    audio = FLAC(str(scratch))
    picture = Picture()
    picture.type, picture.mime, picture.data = 3, "image/png", cover
    audio.add_picture(picture)
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


async def _index(config, track: str = TRACK) -> str:
    await init_db(config.db_path)
    changes = await FileIndexer(config, AsyncIndexerDb(config)).process_file(track)
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
async def test_a_flacs_tags_cost_one_read_of_the_tag_read_ahead(share):
    """Its metadata blocks come first and are read straight through, so
    the 1 MiB read-ahead the embedder wants used to be one read too."""
    client, config = share

    assert await _index(config)

    assert _open_bufferings(client) == [TAG_READ_AHEAD_BYTES]
    [opened] = client.opened
    assert (opened.reads, opened.fetched) == (1, TAG_READ_AHEAD_BYTES)


@pytest.mark.asyncio
async def test_an_mp3s_tags_cost_the_nas_a_few_reads_of_the_tag_read_ahead(
    share, tmp_path, mp3_frames
):
    """mutagen reads the ID3v2 tag at the start, looks for an ID3v1 tag at
    the end, then comes back for the first frames, and coming back filled
    the 1 MiB read-ahead a second time: 2 MB from the NAS a file, for a few
    KB of tags."""
    client, config = share
    mp3 = _tagged_mp3(tmp_path, mp3_frames)
    client.files[MP3_TRACK_UNC] = mp3

    track_id = await _index(config, MP3_TRACK)

    [opened] = client.opened
    assert opened.reads <= 3
    assert opened.fetched < 200_000
    track = await AsyncIndexerDb(config).get_track_by_id(track_id)
    assert (track["title"], track["artist_name"]) == ("Ladder", "The Sines")
    assert track["duration"] == int(MP3(io.BytesIO(mp3)).info.length)


@pytest.mark.parametrize(
    "track,unc,source,tag,reads",
    [
        (MP3_TRACK, MP3_TRACK_UNC, "mp3_frames", _tagged_mp3, 3),
        (TRACK, TRACK_UNC, "noise_flac", _covered_flac, 1),
    ],
    ids=["mp3", "flac"],
)
def test_a_cover_in_the_tags_costs_its_size_and_two_reads_more(
    share, tmp_path, request, track, unc, source, tag, reads
):
    """A tag bigger than the read-ahead is read past it, in one read, and
    what follows it fills the read-ahead again."""
    client, config = share
    client.files[unc] = tag(tmp_path, request.getfixturevalue(source), COVER)
    indexer = FileIndexer(config, AsyncIndexerDb(config))

    metadata = indexer._extract_metadata(indexer.storage.for_path(track), track)

    assert metadata["album_art"] == COVER
    [opened] = client.opened
    assert opened.reads <= reads + 2
    assert opened.fetched <= len(COVER) + (reads + 1) * TAG_READ_AHEAD_BYTES


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
async def test_a_share_gone_mid_track_is_a_503_the_renderer_retries(
    share, one_worker
):
    """The next range is served from the file kept open, without the probe
    that used to find the share gone, and must still answer as it did: a
    renderer retries a 503 but cannot recover a 206 whose body breaks."""
    client, config = share
    track_id = await _index(config)
    module = _module_reading(config, build_resolver(config))
    url = f"{CONTENT_ROUTE}/localfiles/{track_id}"

    async with _serving(module) as http:
        assert (await http.get(url, headers=_ranged(0))).status_code == 206
        gone = SMBConnectionClosed("SMB socket was closed")
        client.failure = gone
        for opened in client.opened:
            opened.failure = gone

        r = await http.get(url, headers=_ranged(RANGE_BYTES))

    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


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
