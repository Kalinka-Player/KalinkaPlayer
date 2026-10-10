"""Real PCM WAV containers, ID3 tags, library discovery and precision."""

import io
import json
import logging
import struct
from types import SimpleNamespace

import aiosqlite
import pytest
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import (
    FileIndexer,
    is_supported_audio_file,
)
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.indexer.wav import UnsupportedWave, open_pcm_wave
from kalinka_plugin_localfiles.storage import media_type_of


def chunk(name, data):
    return name + struct.pack("<I", len(data)) + data + bytes(len(data) % 2)


def fixture(
    bits=24,
    rate=96000,
    channels=2,
    extensible=False,
    width=None,
    encoding=1,
    tagged=False,
):
    width = width or bits
    block = channels * width // 8
    fmt = struct.pack(
        "<HHIIHH",
        0xFFFE if extensible else encoding,
        channels,
        rate,
        rate * block,
        block,
        width,
    )
    if extensible:
        fmt += struct.pack("<HHI", 22, bits, 4 if channels == 1 else 3)
        fmt += bytes([encoding]) + bytes.fromhex("00000000001000800000aa00389b71")
    body = b"WAVE" + chunk(b"fmt ", fmt) + chunk(b"JUNK", b"odd")
    body += chunk(b"data", bytes(rate * block))
    if tagged:
        tags = ID3()
        for frame in (
            TIT2(text=["WAV title"]),
            TPE1(text=["The Artist"]),
            TALB(text=["The Album"]),
            TRCK(text=["3/10"]),
            APIC(mime="image/png", type=3, data=b"cover-data"),
        ):
            tags.add(frame)
        data = io.BytesIO()
        tags.save(data, padding=lambda _: 0)
        body += chunk(b"id3 ", data.getvalue())
    return chunk(b"RIFF", body)


@pytest.mark.parametrize("extension", ["wav", "WAV", "wave"])
@pytest.mark.parametrize("tagged", [False, True])
def test_wav_metadata_and_cover_through_storage(tmp_path, extension, tagged):
    payload = fixture(tagged=tagged)
    storage = SimpleNamespace(
        open=lambda _, **__: io.BytesIO(payload),
        is_file=lambda _: False,
        listdir=lambda _: [],
    )
    path = f"smb://nas/music/Symphony #5.{extension}"
    assert is_supported_audio_file(path)
    assert media_type_of(path) == "audio/wav"
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)
    metadata = fi._extract_metadata(storage, path)
    assert metadata is not None
    assert metadata["duration"] == 1
    assert metadata["format"] == "audio/wav"
    assert metadata["stream_info"]["bits_per_sample"] == 24
    assert metadata["stream_info"]["sample_rate"] == 96000
    assert metadata["stream_info"]["channels"] == 2
    assert metadata["stream_info"]["codec"] == "pcm"
    if tagged:
        assert metadata["title"] == "WAV title"
        assert metadata["artist"] == "The Artist"
        assert metadata["album"] == "The Album"
        assert metadata["track_number"] == 3
        assert metadata["album_art"] == b"cover-data"
        assert fi._embedded_art(storage, path) == b"cover-data"
    else:
        assert "title" not in metadata
        assert fi._embedded_art(storage, path) is None


@pytest.mark.parametrize(
    "bits,width,encoding",
    [
        (16, 16, 1),
        (24, 24, 1),
        (24, 32, 1),
        (32, 32, 1),
        (32, 32, 3),
        (64, 64, 3),
    ],
)
@pytest.mark.parametrize("channels", [1, 2])
def test_extensible_precision_and_encoding(bits, width, encoding, channels):
    wav = open_pcm_wave(
        io.BytesIO(fixture(bits, 192000, channels, True, width, encoding))
    )
    assert wav.info.bits_per_sample == bits
    assert wav.info.sample_rate == 192000
    assert wav.info.audio_format == encoding
    assert wav.info.length == 1.0


@pytest.mark.parametrize("encoding,bits", [(3, 32), (3, 64), (1, 32)])
def test_wide_pcm_metadata(tmp_path, encoding, bits):
    payload = fixture(bits=bits, encoding=encoding)
    storage = SimpleNamespace(
        open=lambda _, **__: io.BytesIO(payload),
        is_file=lambda _: False,
        listdir=lambda _: [],
    )
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)
    metadata = fi._extract_metadata(storage, "wide.wav")
    assert metadata["stream_info"]["bits_per_sample"] == bits
    assert metadata["stream_info"]["codec"] == ("pcm_float" if encoding == 3 else "pcm")


def test_odd_sized_audio_without_its_pad_byte_is_accepted():
    # 24-bit mono, three frames: nine audio bytes and no pad after them.
    body = fixture(bits=24, channels=1, rate=3)
    body = body[: body.index(b"data") + 8 + 9]
    body = b"RIFF" + struct.pack("<I", len(body) - 8) + body[8:]
    wav = open_pcm_wave(io.BytesIO(body))
    assert wav.info.bits_per_sample == 24
    assert wav.info.channels == 1


def test_metadata_past_the_renderer_limit_is_not_indexed():
    body = bytearray(fixture())
    body[7] = body[43] = 0x04  # The JUNK chunk and RIFF claim 64 MB more.
    with pytest.raises(UnsupportedWave, match="header is too large"):
        open_pcm_wave(io.BytesIO(bytes(body)))


@pytest.mark.parametrize(
    "payload",
    [
        b"RIFF",
        fixture(encoding=6),
        fixture(bits=8),
        fixture(channels=6),
        fixture(bits=24, encoding=3),
        fixture()[:100],
        fixture(bits=24, width=16, extensible=True),
    ],
)
def test_unplayable_files_are_not_indexed(tmp_path, payload, caplog):
    storage = SimpleNamespace(open=lambda _, **__: io.BytesIO(payload))
    fi = FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)
    with caplog.at_level(logging.WARNING):
        assert fi._extract_metadata(storage, "bad.wav") is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "Not indexing bad.wav" in caplog.text


@pytest.mark.asyncio
async def test_wav_is_discovered_and_its_precision_saved(tmp_path):
    folder = tmp_path / "The Artist" / "2024 - The Album"
    folder.mkdir(parents=True)
    path = folder / "03 - Track Name.wav"
    path.write_bytes(fixture())
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
                "SELECT title, format FROM tracks WHERE id=?", (result["tracks"],)
            )
        ).fetchone()
        assert track == ("Track Name", "audio/wav")
        evidence = await (
            await conn.execute(
                "SELECT stream_info FROM track_evidence WHERE track_id=?",
                (result["tracks"],),
            )
        ).fetchone()
        assert json.loads(evidence[0])["bits_per_sample"] == 24
