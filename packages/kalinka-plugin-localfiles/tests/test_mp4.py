"""MP4 audio in My Library: the AAC and FLAC tracks the renderer plays, their
tags and cover, how a fragmented file is timed, how one is served, and the
other codecs an MP4 file can carry, which stay out."""

import io
import json
import logging
import mimetypes
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
from kalinka_plugin_localfiles.indexer.indexer import (
    FileIndexer,
    is_supported_audio_file,
)
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.storage import media_type_of
from kalinka_plugin_localfiles.storage.local import LocalStorage

LOCAL = LocalStorage()
RATE = 44100
TRACK_ID = 1

#: AudioSpecificConfigs: object type, rate index, channels, then any
#: SBR extension and the core's object type, then three zero GA flags.
AAC_LC = bytes.fromhex("1210")
HE_AAC = bytes.fromhex("2b118800")
HE_AAC_V2 = bytes.fromhex("eb098800")
AAC_51 = bytes.fromhex("1230")
AAC_MAIN = bytes.fromhex("0a10")

TAGS = {
    "\xa9nam": ["Ladder"],
    "\xa9ART": ["The Sines"],
    "\xa9alb": ["Test Tones"],
    "aART": ["Various Artists"],
    "trkn": [(3, 10)],
    "disk": [(2, 2)],
    "\xa9day": ["2024-05-01"],
    "\xa9gen": ["Electronic"],
    "cpil": True,
    "\xa9too": ["Lavf62"],
    "----:com.apple.iTunes:replaygain_track_gain": [MP4FreeForm(b"-6.50 dB")],
    "----:com.apple.iTunes:replaygain_track_peak": [MP4FreeForm(b"0.988")],
}


def box(name, *payload):
    data = b"".join(payload)
    return struct.pack(">I4s", 8 + len(data), name) + data


def full_box(name, flags, *payload):
    """A version 0 box with ``flags``."""
    return box(name, flags.to_bytes(4, "big"), *payload)


def descriptor(tag, *payload):
    data = b"".join(payload)
    return bytes([tag, len(data)]) + data


def sample_entry(name, child, channels=2, rate=RATE):
    fields = bytes(6) + struct.pack(">H", 1) + bytes(8)
    fields += struct.pack(">HHHHI", channels, 16, 0, 0, rate << 16)
    return box(name, fields, child)


def aac(config=AAC_LC):
    decoder = descriptor(
        0x04,
        bytes([0x40, 0x15]) + bytes(3) + struct.pack(">II", 128000, 128000),
        descriptor(0x05, config),
    )
    es = descriptor(0x03, struct.pack(">HB", 1, 0), decoder, descriptor(0x06, b"\x02"))
    return sample_entry(b"mp4a", full_box(b"esds", 0, es))


def flac(rate=96000, channels=2, bits=24, seconds=2):
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
    packed |= rate * seconds
    stream_info = struct.pack(">HH", 4096, 4096) + bytes(6)
    stream_info += packed.to_bytes(8, "big") + bytes(16)
    blocks = b"\x80" + len(stream_info).to_bytes(3, "big") + stream_info
    # The sample entry's rate field cannot hold 96000.
    return sample_entry(b"fLaC", full_box(b"dfLa", 0, blocks), channels, rate=0)


def alac():
    cookie = struct.pack(">IBBBBBBHIII", 4096, 0, 16, 40, 10, 14, 2, 255, 0, 0, RATE)
    return sample_entry(b"alac", full_box(b"alac", 0, cookie))


def opus():
    head = struct.pack(">BBHIhB", 0, 2, 312, 48000, 0, 0)
    return sample_entry(b"Opus", box(b"dOps", head))


def track(entry, handler=b"soun", ticks=RATE * 2, stts=None, track_id=TRACK_ID):
    """A track whose media is timed in samples at 44.1 kHz, ``ticks`` long,
    its sample table listing ``stts`` runs of (samples, duration)."""
    stts = [(ticks // 1024, 1024)] if stts is None else stts
    tkhd = full_box(b"tkhd", 7, struct.pack(">III", 0, 0, track_id), bytes(68))
    mdhd = full_box(b"mdhd", 0, struct.pack(">IIIIHH", 0, 0, RATE, ticks, 0, 0))
    hdlr = full_box(b"hdlr", 0, bytes(4), handler, bytes(13))
    runs = b"".join(struct.pack(">II", *run) for run in stts)
    stbl = box(
        b"stbl",
        full_box(b"stsd", 0, struct.pack(">I", 1), entry),
        full_box(b"stts", 0, struct.pack(">I", len(stts)), runs),
    )
    return box(b"trak", tkhd, box(b"mdia", mdhd, hdlr, box(b"minf", stbl)))


def mp4(*traks, mvex=b"", fragments=b""):
    mvhd = full_box(b"mvhd", 0, struct.pack(">IIII", 0, 0, 1000, 2000), bytes(80))
    return (
        box(b"ftyp", b"M4A ", bytes(4), b"M4A isom")
        + box(b"moov", mvhd, *traks, mvex)
        + fragments
        + box(b"mdat", bytes(64))
    )


def trex(default_duration, track_id=TRACK_ID):
    return full_box(
        b"trex", 0, struct.pack(">IIIII", track_id, 1, default_duration, 0, 0)
    )


def fragment(trun, tfhd_flags=0, default_duration=None, track_id=TRACK_ID):
    fields = struct.pack(">I", track_id)
    if tfhd_flags & 0x01:
        fields += struct.pack(">Q", 0)
    if default_duration is not None:
        tfhd_flags |= 0x08
        fields += struct.pack(">I", default_duration)
    traf = box(b"traf", full_box(b"tfhd", tfhd_flags, fields), trun)
    return box(b"moof", full_box(b"mfhd", 0, struct.pack(">I", 1)), traf)


def run_of(durations):
    """A run that lists each sample's duration and size after its data offset."""
    samples = b"".join(struct.pack(">II", duration, 100) for duration in durations)
    return full_box(b"trun", 0x301, struct.pack(">Ii", len(durations), 0), samples)


def run_of_count(count):
    """A run that lists only each sample's size, its duration the default."""
    samples = struct.pack(">I", 100) * count
    return full_box(b"trun", 0x205, struct.pack(">IiI", count, 0, 0), samples)


def tagged(path, data, cover=None):
    path.write_bytes(data)
    audio = MP4(path)
    audio.add_tags()
    for key, value in TAGS.items():
        audio[key] = value
    if cover is not None:
        audio["covr"] = [MP4Cover(b"", MP4Cover.FORMAT_PNG), cover]
    audio.save()
    return path


def _indexer(tmp_path):
    return FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)


@pytest.mark.parametrize(
    "name", ["track.m4a", "TRACK.M4A", "smb://nas/music/Symphony #5.m4a"]
)
def test_m4a_files_are_indexed_as_audio_mp4(name):
    assert is_supported_audio_file(name)
    assert media_type_of(name) == "audio/mp4"


def test_mp4_files_are_not_since_most_are_video():
    assert not is_supported_audio_file("clip.mp4")


def test_tags_cover_and_stream_of_an_aac_file(tmp_path):
    cover = b"\x89PNG cover"
    path = tagged(tmp_path / "ladder.m4a", mp4(track(aac())), cover)
    fi = _indexer(tmp_path)

    metadata = fi._extract_metadata(LOCAL, str(path))

    assert metadata["format"] == "audio/mp4"
    assert metadata["title"] == "Ladder"
    assert metadata["artist"] == "The Sines"
    assert metadata["album"] == "Test Tones"
    assert metadata["track_number"] == 3
    assert metadata["disc_number"] == 2
    assert metadata["year"] == 2024
    assert metadata["genre"] == "Electronic"
    assert metadata["replaygain_gain"] == -6.5
    assert metadata["replaygain_peak"] == 0.988
    assert metadata["duration"] == 2
    assert metadata["album_art"] == cover
    assert fi._embedded_art(LOCAL, str(path)) == cover
    raw = metadata["raw_tags"]
    assert raw["albumartist"] == ["Various Artists"]
    assert raw["compilation"] == ["1"]
    assert raw["tracknumber"] == ["3/10"]
    assert raw["date"] == ["2024-05-01"]
    assert "covr" not in raw
    assert metadata["stream_info"] == {
        "sample_rate": RATE,
        "bits_per_sample": None,
        "channels": 2,
        "codec": "aac",
        "encoder": "Lavf62",
        "bitrate": 128000,
        "container": "mp4",
    }


def test_an_untagged_file_has_no_tags_or_cover(tmp_path):
    path = tmp_path / "ladder.m4a"
    path.write_bytes(mp4(track(aac())))
    fi = _indexer(tmp_path)

    metadata = fi._extract_metadata(LOCAL, str(path))

    assert metadata["duration"] == 2
    assert metadata["raw_tags"] == {}
    assert "title" not in metadata
    assert "album_art" not in metadata
    assert fi._embedded_art(LOCAL, str(path)) is None


@pytest.mark.parametrize(
    "config,channels",
    [(HE_AAC, 2), (HE_AAC_V2, 2), (AAC_51, 6)],
    ids=["HE-AAC", "HE-AAC v2", "5.1 AAC-LC"],
)
def test_he_aac_and_surround_aac_are_indexed(tmp_path, config, channels):
    path = tmp_path / "track.m4a"
    path.write_bytes(mp4(track(aac(config))))

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["stream_info"]["codec"] == "aac"
    assert metadata["stream_info"]["channels"] == channels


def test_flac_is_described_by_its_streaminfo(tmp_path):
    path = tmp_path / "track.m4a"
    path.write_bytes(mp4(track(flac(rate=96000, bits=24))))

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    stream = metadata["stream_info"]
    assert stream["codec"] == "flac"
    assert stream["sample_rate"] == 96000
    assert stream["bits_per_sample"] == 24
    assert stream["channels"] == 2
    assert stream["bitrate"] is None


@pytest.mark.parametrize("bits", [16, 32])
@pytest.mark.parametrize("channels", [1, 2])
def test_flac_in_mono_or_stereo_at_any_depth_the_renderer_takes(
    tmp_path, bits, channels
):
    path = tmp_path / "track.m4a"
    path.write_bytes(mp4(track(flac(channels=channels, bits=bits))))

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["stream_info"]["bits_per_sample"] == bits
    assert metadata["stream_info"]["channels"] == channels


@pytest.mark.parametrize(
    "payload,reason",
    [
        (mp4(track(alac())), "ALAC"),
        (mp4(track(opus())), "OPUS"),
        (mp4(track(aac(AAC_MAIN))), "AAC MAIN"),
        (mp4(track(flac(channels=6))), "6 channels"),
        (mp4(track(flac(bits=20))), "20-bit"),
        (mp4(track(aac(), handler=b"vide")), "no audio track"),
        (mp4(track(sample_entry(b"fLaC", box(b"free")))), "no STREAMINFO"),
    ],
    ids=[
        "ALAC",
        "Opus",
        "AAC Main",
        "5.1 FLAC",
        "20-bit FLAC",
        "video only",
        "FLAC without STREAMINFO",
    ],
)
def test_unplayable_files_are_not_indexed(tmp_path, payload, reason, caplog):
    storage = SimpleNamespace(open=lambda _, **__: io.BytesIO(payload))
    with caplog.at_level(logging.DEBUG, logger="indexer"):
        assert _indexer(tmp_path)._extract_metadata(storage, "bad.m4a") is None

    [record] = [r for r in caplog.records if r.name == "indexer"]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Not indexing bad.m4a" in record.getMessage()
    assert reason in record.getMessage()


def test_a_file_that_is_no_mp4_is_not_blamed_on_its_codec(tmp_path, caplog):
    path = tmp_path / "track.m4a"
    path.write_bytes(b"ID3" + bytes(64))

    with caplog.at_level(logging.DEBUG, logger="indexer"):
        assert _indexer(tmp_path)._extract_metadata(LOCAL, str(path)) is None

    [record] = [r for r in caplog.records if r.name == "indexer"]
    assert record.levelno == logging.ERROR


@pytest.mark.parametrize(
    "fragments",
    [
        [fragment(run_of([1024] * 43)), fragment(run_of([1024] * 43 + [1012]))],
        [fragment(run_of_count(43), tfhd_flags=0x01, default_duration=1024)] * 2
        + [fragment(run_of([1012]))],
        [fragment(run_of_count(43))] * 2 + [fragment(run_of([1012]))],
    ],
    ids=["durations listed", "default from tfhd", "default from trex"],
)
def test_a_fragmented_file_is_timed_over_its_fragments(tmp_path, fragments):
    """mutagen reads the movie header's samples alone: none, here."""
    path = tmp_path / "track.m4a"
    path.write_bytes(
        mp4(
            track(aac(), ticks=0, stts=[]),
            mvex=box(b"mvex", trex(1024)),
            fragments=b"".join(fragments),
        )
    )

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["duration"] == 2
    assert MP4(path).info.length == 0


def test_a_fragmented_files_movie_header_samples_count_too(tmp_path):
    path = tmp_path / "track.m4a"
    path.write_bytes(
        mp4(
            track(flac(), ticks=RATE, stts=[(1, RATE)]),
            mvex=box(b"mvex", trex(1024)),
            fragments=fragment(run_of_count(87)),
        )
    )

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["duration"] == 3


def test_another_tracks_fragments_are_not_counted(tmp_path):
    path = tmp_path / "track.m4a"
    path.write_bytes(
        mp4(
            track(aac(), ticks=0, stts=[]),
            mvex=box(b"mvex", trex(1024), trex(1024, track_id=2)),
            fragments=fragment(run_of([1024] * 87))
            + fragment(run_of([RATE] * 10), track_id=2),
        )
    )

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["duration"] == 2


@pytest.mark.asyncio
async def test_an_indexed_m4a_track_is_served_as_audio_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    music = tmp_path / "music"
    music.mkdir()
    path = tagged(music / "03 Ladder.m4a", mp4(track(aac())))
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
    [served] = await module.get_track_info([track_id])

    assert info.mime_type == "audio/mp4"
    assert info.local_path == str(path)
    assert (await served.source_retriever()).format == "audio/mp4"
    async with aiosqlite.connect(config.db_path) as conn:
        [stream_info] = await (
            await conn.execute(
                "SELECT stream_info FROM track_evidence WHERE track_id=?",
                (track_id,),
            )
        ).fetchone()
    assert json.loads(stream_info)["codec"] == "aac"


@pytest.mark.parametrize(
    "path,reason",
    [
        ("/m/t.m4a", "libsndfile cannot decode MP4 audio for analysis"),
        ("smb://nas/m/t.AAC", "libsndfile cannot decode AAC audio for analysis"),
    ],
)
@pytest.mark.asyncio
async def test_aac_audio_analysis_is_terminal_and_never_opens_the_file(path, reason):
    """libsndfile, which the audio embedding reads PCM through, has no AAC."""
    worker = EmbeddingWorker.__new__(EmbeddingWorker)
    worker._audio_available = True
    worker.config = LocalFilesConfig()
    worker.storage = Mock()
    worker.db = SimpleNamespace(
        claim_batch=AsyncMock(return_value=[{"entity_id": "t", "id": 1}]),
        get_file_path_for_track=AsyncMock(return_value=path),
        fail_job=AsyncMock(),
    )

    assert await worker._process_clap_batch()

    worker.db.fail_job.assert_awaited_once_with(1, reason, 1)
    assert worker._compute_clap_audio(path) is None
    assert worker.storage.mock_calls == []
