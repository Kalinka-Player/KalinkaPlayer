"""Raw AAC in My Library: ADTS streams of the profiles the renderer plays,
the ID3 tag one may start with, how one is served, and the files named .aac
that hold something else, which stay out."""

import io
import logging
import mimetypes
from types import SimpleNamespace

import pytest
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import (
    FileIndexer,
    is_supported_audio_file,
)
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.storage import media_type_of

#: 44.1 kHz, the index ADTS gives it.
RATE_44100 = 4
#: Just over two seconds at 1024 samples a frame.
FRAMES = 87
LC, MAIN, SSR = 1, 0, 2


def frame(profile=LC, rate=RATE_44100, channels=2, payload=200, blocks=0):
    """One ADTS frame without a CRC, of ``payload`` silent bytes."""
    length = 7 + payload
    bits = 0xFFF << 44 | 1 << 40 | profile << 38 | rate << 34 | channels << 30
    bits |= length << 13 | 0x7FF << 2 | blocks
    return bits.to_bytes(7, "big") + bytes(payload)


def stream(*frames):
    return b"".join(frames or [frame()] * FRAMES)


def id3(*frames):
    tag = ID3()
    for f in frames:
        tag.add(f)
    data = io.BytesIO()
    tag.save(data, padding=lambda _: 0)
    return data.getvalue()


def storage_of(payload):
    return SimpleNamespace(
        open=lambda _, **__: io.BytesIO(payload),
        is_file=lambda _: False,
        listdir=lambda _: [],
    )


def _indexer(tmp_path):
    return FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)


@pytest.mark.parametrize(
    "name", ["track.aac", "TRACK.AAC", "smb://nas/music/Symphony #5.aac"]
)
def test_aac_files_are_indexed_as_audio_aac(name):
    assert is_supported_audio_file(name)
    assert media_type_of(name) == "audio/aac"


def test_tags_cover_and_stream_of_a_tagged_stream(tmp_path):
    tag = id3(
        TIT2(text=["AAC title"]),
        TPE1(text=["The Artist"]),
        TALB(text=["The Album"]),
        TRCK(text=["3/10"]),
        APIC(mime="image/png", type=3, data=b"cover-data"),
    )
    storage = storage_of(tag + stream())
    fi = _indexer(tmp_path)

    metadata = fi._extract_metadata(storage, "smb://nas/music/Track #1.aac")

    assert metadata["format"] == "audio/aac"
    assert metadata["title"] == "AAC title"
    assert metadata["artist"] == "The Artist"
    assert metadata["album"] == "The Album"
    assert metadata["track_number"] == 3
    assert metadata["duration"] == 2
    assert metadata["album_art"] == b"cover-data"
    assert fi._embedded_art(storage, "smb://nas/music/Track #1.aac") == b"cover-data"
    stream_info = metadata["stream_info"]
    assert stream_info["codec"] == "aac"
    assert stream_info["sample_rate"] == 44100
    assert stream_info["channels"] == 2
    assert stream_info["bitrate"] == 200 * 8 * 44100 // 1024


def test_an_untagged_stream_has_no_tags_or_cover(tmp_path):
    storage = storage_of(stream())
    fi = _indexer(tmp_path)

    metadata = fi._extract_metadata(storage, "track.aac")

    assert metadata["duration"] == 2
    assert metadata["raw_tags"] == {}
    assert "title" not in metadata
    assert "album_art" not in metadata
    assert fi._embedded_art(storage, "track.aac") is None


def test_a_stream_shorter_than_the_frames_checked_is_indexed(tmp_path):
    """mutagen times a stream from three frames, which is all it has."""
    metadata = _indexer(tmp_path)._extract_metadata(
        storage_of(stream(*[frame()] * 3)), "track.aac"
    )

    assert metadata["stream_info"]["codec"] == "aac"


MP3_FRAME = b"\xff\xfb\x90\x00" + bytes(413)


@pytest.mark.parametrize(
    "payload,reason",
    [
        (b"", "not an ADTS stream"),
        (MP3_FRAME * 5, "not an ADTS stream"),
        (id3(TIT2(text=["MP3"])) + MP3_FRAME * 5, "not an ADTS stream"),
        (b"ADIF" + bytes(64), "not an ADTS stream"),
        (bytes(4) + stream(), "not an ADTS stream"),
        (stream(frame(), frame(rate=3), frame()), "not an ADTS stream"),
        (stream(frame(), frame(payload=0)[:5]), "not an ADTS stream"),
        (stream(*[frame(profile=MAIN)] * 5), "AAC-LC or HE-AAC"),
        (stream(*[frame(profile=SSR)] * 5), "AAC-LC or HE-AAC"),
        (stream(*[frame(channels=0)] * 5), "channel layout"),
        (stream(*[frame(blocks=1)] * 5), "several AAC blocks"),
    ],
    ids=[
        "empty",
        "MP3",
        "tagged MP3",
        "ADIF",
        "bytes before the first frame",
        "frames of two streams",
        "truncated second frame",
        "AAC Main",
        "AAC SSR",
        "channels in a program config element",
        "several blocks a frame",
    ],
)
def test_unplayable_files_are_not_indexed(tmp_path, payload, reason, caplog):
    with caplog.at_level(logging.DEBUG, logger="indexer"):
        assert (
            _indexer(tmp_path)._extract_metadata(storage_of(payload), "bad.aac") is None
        )

    [record] = [r for r in caplog.records if r.name == "indexer"]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Not indexing bad.aac" in record.getMessage()
    assert reason in record.getMessage()


@pytest.mark.asyncio
async def test_an_indexed_aac_track_is_served_as_audio_aac(tmp_path, monkeypatch):
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    music = tmp_path / "music"
    music.mkdir()
    path = music / "03 Ladder.aac"
    path.write_bytes(id3(TIT2(text=["Ladder"])) + stream())
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    track_id = (
        await FileIndexer(config, AsyncIndexerDb(config)).process_file(str(path))
    )["tracks"]
    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))

    info = await module.get_content_info(track_id)
    [track] = await module.get_track_info([track_id])

    assert info.mime_type == "audio/aac"
    assert info.local_path == str(path)
    assert (await track.source_retriever()).format == "audio/aac"
