"""Real DSF/DSDIFF containers: tags, provenance, storage and no PCM analysis."""

import io
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TDRC, APIC, TXXX

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import (
    FileIndexer,
    is_supported_audio_file,
)
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.storage import media_type_of
from kalinka_plugin_localfiles.enricher.acoustid_plugin import AcoustIdPlugin
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker


def tags():
    result = ID3()
    for frame in [
        TIT2(text=["Tagged title"]),
        TPE1(text=["The Artist"]),
        TALB(text=["The Album"]),
        TPE2(text=["Album Artist"]),
        TRCK(text=["3/10"]),
        TPOS(text=["2/2"]),
        TDRC(text=["2024"]),
        TXXX(desc="MusicBrainz Track Id", text=["recording-mbid"]),
        APIC(mime="image/png", type=3, data=b"cover-data"),
    ]:
        result.add(frame)
    stream = io.BytesIO()
    result.save(stream, padding=lambda _: 0)
    return stream.getvalue()


def fixture(kind, tagged=True, order=1, rate=2822400):
    metadata = tags() if tagged else b""
    if kind == "dsf":
        payload = b"\x69" * 8192
        offset = 92 + len(payload)
        return (
            b"DSD "
            + struct.pack("<QQQ", 28, offset + len(metadata), offset if tagged else 0)
            + b"fmt "
            + struct.pack("<QIIIIIIQII", 52, 1, 0, 2, 2, rate, order, 32768, 4096, 0)
            + b"data"
            + struct.pack("<Q", len(payload) + 12)
            + payload
            + metadata
        )

    def chunk(name, data):
        return (
            name
            + struct.pack(">Q", len(data))
            + data
            + (b"\x00" if len(data) % 2 else b"")
        )

    prop = (
        b"SND "
        + chunk(b"FS  ", struct.pack(">I", rate))
        + chunk(b"CHNL", struct.pack(">H", 2) + b"SLFTSRGT")
        + chunk(b"CMPR", b"DSD \x00")
    )
    body = b"DSD " + chunk(b"PROP", prop) + chunk(b"DSD ", b"\x69" * 8192)
    if tagged:
        body += chunk(b"ID3 ", metadata)
    return chunk(b"FRM8", body)


@pytest.mark.parametrize("kind", ["dsf", "dff"])
@pytest.mark.parametrize("tagged", [True, False])
def test_extracts_tags_and_artwork_over_storage(tmp_path, kind, tagged):
    payload = fixture(kind, tagged)
    storage = SimpleNamespace(
        open=lambda _: io.BytesIO(payload),
        is_file=lambda _: False,
        listdir=lambda _: [],
    )
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)
    path = f"smb://nas/music/Symphony #5.{kind.upper()}"
    assert is_supported_audio_file(path)
    assert media_type_of(path) == f"audio/x-{kind}"
    metadata = fi._extract_metadata(storage, path)
    assert metadata is not None
    assert metadata["stream_info"]["bits_per_sample"] == 1
    assert metadata["stream_info"]["sample_rate"] == 2822400
    assert metadata["stream_info"]["channels"] == 2
    assert metadata["stream_info"]["codec"] == "dsd"
    assert metadata["duration"] == 0
    if tagged:
        assert metadata["title"] == "Tagged title"
        assert metadata["artist"] == "The Artist"
        assert metadata["album"] == "The Album"
        assert metadata["track_number"] == 3
        assert metadata["disc_number"] == 2
        assert metadata["year"] == 2024
        assert metadata["raw_tags"]["TPE2"] == "Album Artist"
        assert metadata["raw_tags"]["TXXX:MusicBrainz Track Id"] == "recording-mbid"
        assert metadata["album_art"] == b"cover-data"
        assert fi._embedded_art(storage, path) == b"cover-data"
    else:
        assert "title" not in metadata
        assert fi._embedded_art(storage, path) is None


@pytest.mark.parametrize("order", [1, 8])
def test_dsf_bit_order_is_not_audio_precision(tmp_path, order):
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)
    storage = SimpleNamespace(
        open=lambda _: io.BytesIO(fixture("dsf", order=order, rate=11289600)),
        is_file=lambda _: False,
        listdir=lambda _: [],
    )
    metadata = fi._extract_metadata(storage, "/music/test.dsf")
    assert metadata["stream_info"]["bits_per_sample"] == 1
    assert metadata["stream_info"]["sample_rate"] == 11289600
    assert metadata["stream_info"]["bitrate"] == 22579200


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dsf", "dff"])
async def test_indexing_preserves_evidence_and_filename_fallback(tmp_path, kind):
    folder = tmp_path / "The Artist" / "2024 - The Album"
    folder.mkdir(parents=True)
    path = folder / f"03 - Track Name.{kind}"
    path.write_bytes(fixture(kind, tagged=False))
    cfg = LocalFilesConfig(
        music_folders=[str(tmp_path)],
        db_path=str(tmp_path / "db"),
        artwork_path=str(tmp_path / "art"),
        quiescence_seconds=0,
    )
    await init_db(cfg.db_path)
    fi = FileIndexer(cfg, AsyncIndexerDb(cfg))
    result = await fi.process_file(str(path))
    assert result["tracks"]
    async with aiosqlite.connect(cfg.db_path) as conn:
        track = await (
            await conn.execute(
                "SELECT title, file_path, format FROM tracks WHERE id=?",
                (result["tracks"],),
            )
        ).fetchone()
        assert track[0] == "Track Name"
        assert track[1] == str(path)
        assert track[2] == f"audio/x-{kind}"
        evidence = await (
            await conn.execute(
                "SELECT raw_tags, stream_info FROM track_evidence WHERE track_id=?",
                (result["tracks"],),
            )
        ).fetchone()
        assert json.loads(evidence[0]) == {}
        assert json.loads(evidence[1])["codec"] == "dsd"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,media",
    [
        ("/music/t.DSF", ""),
        ("smb://nas/music/t.dff", ""),
        ("/music/unknown", "audio/x-dsf"),
    ],
)
async def test_acoustid_does_not_read_stage_fingerprint_or_lookup_dsd(path, media):
    plugin = AcoustIdPlugin.__new__(AcoustIdPlugin)
    plugin.storage = Mock()
    plugin._run_fpcalc = Mock(side_effect=AssertionError("must not decode DSD"))
    plugin._lookup_fingerprint = Mock(side_effect=AssertionError("must not lookup DSD"))
    assert await plugin.enrich_track({"file_path": path, "format": media}) is None
    if path.lower().endswith((".dsf", ".dff")):
        assert plugin._generate_fingerprint(path) == (None, None)
    assert plugin.storage.mock_calls == []
    plugin._run_fpcalc.assert_not_called()
    plugin._lookup_fingerprint.assert_not_called()


@pytest.mark.parametrize("path", ["/music/t.DSF", "smb://nas/music/t.dff"])
def test_audio_embedding_never_opens_or_decodes_dsd(path):
    worker = EmbeddingWorker.__new__(EmbeddingWorker)
    worker.storage = Mock()
    assert worker._compute_clap_audio(path) is None
    assert worker.storage.mock_calls == []


@pytest.mark.asyncio
async def test_dsd_audio_analysis_is_terminal_without_affecting_text_jobs():
    worker = EmbeddingWorker.__new__(EmbeddingWorker)
    worker._audio_available = True
    worker.config = LocalFilesConfig()
    worker.db = SimpleNamespace(
        claim_batch=AsyncMock(return_value=[{"entity_id": "t", "id": 1}]),
        get_file_path_for_track=AsyncMock(return_value="/m/t.dsf"),
        fail_job=AsyncMock(),
    )
    assert await worker._process_clap_batch()
    worker.db.fail_job.assert_awaited_once_with(
        1, "DSD audio analysis requires PCM conversion", 1
    )


@pytest.mark.parametrize("tagged", [False, True])
def test_dff_native_artist_title_and_id3_precedence(tmp_path, tagged):
    def chunk(name, data):
        return (
            name
            + struct.pack(">Q", len(data))
            + data
            + (b"\0" if len(data) % 2 else b"")
        )

    body = fixture("dff", tagged)[12:]
    native = chunk(b"DIAR", struct.pack(">I", 13) + b"Native artist")
    native += chunk(b"DITI", struct.pack(">I", 12) + b"Native title")
    data = chunk(b"FRM8", body + chunk(b"DIIN", native))
    storage = SimpleNamespace(open=lambda _: io.BytesIO(data), listdir=lambda _: [])
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)
    meta = fi._extract_metadata(storage, "/music/test.dff")
    assert meta["artist"] == ("The Artist" if tagged else "Native artist")
    assert meta["title"] == ("Tagged title" if tagged else "Native title")
    assert meta["raw_tags"]["DIAR"] == "Native artist"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dsf", "dff"])
async def test_musicbrainz_recording_search_still_enriches_dsd(monkeypatch, kind):
    from kalinka_plugin_localfiles.enricher import musicbrainz_plugin as module

    plugin = module.MusicBrainzPlugin(LocalFilesConfig(), db_manager=Mock())
    plugin._lookup_track_in_accepted_map = AsyncMock(return_value=None)
    query = AsyncMock(
        return_value={
            "recording-list": [
                {
                    "id": "recording-id",
                    "title": "A song",
                    "ext:score": "100",
                    "length": "120000",
                }
            ]
        }
    )
    monkeypatch.setattr(module, "mb_call", query)
    result = await plugin.enrich_track(
        {
            "id": "t",
            "title": "A song",
            "artist_name": "Artist",
            "album_title": "Album",
            "duration": 120,
            "file_path": f"/music/track.{kind}",
            "format": f"audio/x-{kind}",
        }
    )
    assert result["mbid"] == "recording-id"
    query.assert_awaited_once()
