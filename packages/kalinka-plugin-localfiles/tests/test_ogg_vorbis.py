"""Ogg Vorbis in My Library: what is indexed, its tags and cover, how it is
served, and the other codecs an Ogg file can carry, which the renderer cannot
play and so stay out."""

import base64
import io
import logging
import mimetypes
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.flac import Picture
from mutagen.ogg import OggPage
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import (
    FileIndexer,
    is_supported_audio_file,
)
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.indexer.ogg import (
    _mode_blockflags,
    starts_vorbis_stream,
    vorbis_length,
)
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.storage.local import LocalStorage

LADDER = (
    Path(__file__).resolve().parents[2]
    / "kalinka-renderer"
    / "tests"
    / "data"
    / "ladder.ogg"
)
LADDER_RATE = 22050
#: What libvorbisfile's ov_pcm_total gives for ladder.ogg, as the renderer's
#: VorbisStreamDecoder test checks.
LADDER_FRAMES = LADDER_RATE * 6
LOCAL = LocalStorage()
TAGS = {
    "title": "Ladder",
    "artist": "The Sines",
    "album": "Test Tones",
    "albumartist": "Various Artists",
    "tracknumber": "3/10",
    "discnumber": "2/2",
    "date": "2024-05-01",
    "genre": "Electronic",
    "replaygain_track_gain": "-6.50 dB",
    "replaygain_track_peak": "0.988",
}


def _cover_png(color):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


def _picture_comment(kind, data):
    picture = Picture()
    picture.type = kind
    picture.mime = "image/png"
    picture.data = data
    return base64.b64encode(picture.write()).decode("ascii")


def _vorbis(path, pictures=()):
    shutil.copyfile(LADDER, path)
    audio = OggVorbis(path)
    for key, value in TAGS.items():
        audio[key] = value
    if pictures:
        audio["metadata_block_picture"] = list(pictures)
    audio.save()
    return path


def _ogg(*packets, end):
    """One logical stream, a packet a page, the last page ending at ``end``."""
    pages = []
    for sequence, packet in enumerate(packets):
        page = OggPage()
        page.serial = 1
        page.sequence = sequence
        page.packets = [packet]
        page.first = sequence == 0
        page.last = sequence == len(packets) - 1
        page.position = end if page.last else 0
        pages.append(page.write())
    return b"".join(pages)


def _opus():
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 312, 48000, 0, 0)
    tags = b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0)
    data = _ogg(head, tags, b"\xfc\xff\xfe", end=48312)
    assert OggOpus(io.BytesIO(data)).info.channels == 2
    return data


def _ogg_flac():
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    stream_info = struct.pack(">HH", 4096, 4096) + bytes(6)
    stream_info += packed.to_bytes(8, "big") + bytes(16)
    head = b"\x7fFLAC\x01\x00" + struct.pack(">H", 1) + b"fLaC"
    head += b"\x00" + len(stream_info).to_bytes(3, "big") + stream_info
    comment = struct.pack("<I", 4) + b"test" + struct.pack("<I", 0)
    comments = b"\x84" + len(comment).to_bytes(3, "big") + comment
    data = _ogg(head, comments, b"\xff\xf8", end=44100)
    assert OggFLAC(io.BytesIO(data)).info.sample_rate == 44100
    return data


def _indexer(tmp_path):
    return FileIndexer(LocalFilesConfig(db_path=str(tmp_path / "db")), None)


def _pages(data):
    stream = io.BytesIO(data)
    while True:
        try:
            yield OggPage(stream)
        except EOFError:
            return


def _reserialed(data, serial):
    """The same stream under another serial number, as a later link."""
    pages = list(_pages(data))
    for page in pages:
        page.serial = serial
    return b"".join(page.write() for page in pages)


def _recorded_mid_stream(data, frames):
    """The same stream as if its recording began ``frames`` into it: every
    audio page's position raised by that much."""
    pages = list(_pages(data))
    for page in pages:
        if page.position > 0:
            page.position += frames
    return b"".join(page.write() for page in pages)


def _setup_header(data):
    return next(
        packet
        for packet in OggPage.to_packets(list(_pages(data)))
        if packet.startswith(b"\x05vorbis")
    )


def _with_setup_body_zeroed(data):
    pages = list(_pages(data))
    for page in pages:
        page.packets = [
            packet[:7] + bytes(len(packet) - 7)
            if packet.startswith(b"\x05vorbis")
            else packet
            for packet in page.packets
        ]
    return b"".join(page.write() for page in pages)


def _setup_with_modes(flags):
    """A setup header that ends in a mode table of ``flags``, after bytes
    standing in for its codebooks, floors and residues."""
    fields = [(len(flags) - 1, 6)]
    for mapping, flag in enumerate(flags):
        fields += [(flag, 1), (0, 16), (0, 16), (mapping % 2, 8)]
    fields.append((1, 1))
    head = b"\x05vorbis" + b"\xa5" * 32
    bits, at = int.from_bytes(head, "little"), len(head) * 8
    for value, width in fields:
        bits |= value << at
        at += width
    return bits.to_bytes((at + 7) // 8, "little")


def _identification(sample_rate):
    """A Vorbis identification header: stereo, blocks of 256 and 2048."""
    fields = struct.pack("<IBI3iBB", 0, 2, sample_rate, 0, 0, 0, 0xB8, 1)
    return b"\x01vorbis" + fields


def _timed_link(serial, sample_rate, pages):
    """A Vorbis link of ``pages`` tenth-of-a-second pages with no real audio
    in them, which is all timing it needs."""
    head = OggPage()
    head.serial, head.first = serial, True
    head.packets = [_identification(sample_rate)]
    out = [head.write()]
    for n in range(1, pages + 1):
        page = OggPage()
        page.serial, page.sequence, page.packets = serial, n, [bytes(4000)]
        page.position = n * sample_rate // 10
        page.last = n == pages
        out.append(page.write())
    return b"".join(out)


#: Audio packets in mode 0 and mode 1 of a link from ``_link_pages``.
SHORT, LONG = b"\x00", b"\x02"


def _page(packets, position=-1, continued=False, complete=True):
    page = OggPage()
    page.packets, page.position = packets, position
    page.continued, page.complete = continued, complete
    return page


def _link_pages(modes, audio_pages):
    """The pages of a 22050 Hz link whose setup header holds ``modes``,
    then ``audio_pages``."""
    head = OggPage()
    head.first, head.packets = True, [_identification(22050)]
    comment = b"\x03vorbis" + struct.pack("<II", 0, 0) + b"\x01"
    headers = _page([comment, _setup_with_modes(modes)], position=0)
    pages = [head, headers, *audio_pages]
    for sequence, page in enumerate(pages):
        page.serial, page.sequence = 1, sequence
    return pages


def _decoded(*blocks):
    """What audio packets of ``blocks`` samples each decode to, as
    libvorbisfile counts them: nothing for the first."""
    return sum((before + block) // 4 for before, block in zip(blocks, blocks[1:]))


def _written(pages, damaged=None):
    """``pages`` as a file, the one at index ``damaged`` failing its checksum."""
    out = [page.write() for page in pages]
    if damaged is not None:
        out[damaged] = out[damaged][:-1] + bytes([out[damaged][-1] ^ 0xFF])
    return b"".join(out)


class _CountingFile(io.BytesIO):
    read_bytes = 0
    reads = 0

    def read(self, size=-1):
        data = super().read(size)
        self.read_bytes += len(data)
        self.reads += 1
        return data


@pytest.mark.parametrize(
    "name", ["track.ogg", "TRACK.OGA", "smb://nas/music/Symphony #5.ogg"]
)
def test_ogg_files_are_indexed(name):
    assert is_supported_audio_file(name)


def test_a_macos_fork_beside_one_is_not():
    assert not is_supported_audio_file("._track.ogg")


def test_tags_and_stream_of_a_vorbis_file(tmp_path):
    path = _vorbis(tmp_path / "ladder.ogg")

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["format"] == "audio/ogg"
    assert metadata["title"] == "Ladder"
    assert metadata["artist"] == "The Sines"
    assert metadata["album"] == "Test Tones"
    assert metadata["track_number"] == 3
    assert metadata["disc_number"] == 2
    assert metadata["year"] == 2024
    assert metadata["genre"] == "Electronic"
    assert metadata["replaygain_gain"] == -6.5
    assert metadata["replaygain_peak"] == 0.988
    assert metadata["duration"] == 6
    assert metadata["raw_tags"]["albumartist"] == ["Various Artists"]
    assert metadata["raw_tags"]["tracknumber"] == ["3/10"]
    assert "album_art" not in metadata
    stream = metadata["stream_info"]
    assert stream["codec"] == "vorbis"
    assert stream["sample_rate"] == 22050
    assert stream["channels"] == 2
    assert stream["bits_per_sample"] is None
    assert stream["bitrate"] > 0


def test_a_header_that_gives_no_bitrate_records_none(tmp_path):
    """mutagen reads the missing fields as a bitrate of 0."""
    data = _vorbis(tmp_path / "ladder.ogg").read_bytes()
    stream = io.BytesIO(data)
    ident = OggPage(stream)
    packet = ident.packets[0]
    ident.packets = [packet[:16] + bytes(12) + packet[28:]]
    path = tmp_path / "unrated.ogg"
    path.write_bytes(ident.write() + data[stream.tell() :])

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["duration"] == 6
    assert metadata["stream_info"]["bitrate"] is None


def test_the_front_cover_comes_from_a_picture_comment(tmp_path):
    """A picture that will not decode is passed over rather than costing the
    track its tags; the front cover wins over a picture listed before it."""
    front = _cover_png((200, 40, 0))
    path = _vorbis(
        tmp_path / "ladder.ogg",
        pictures=[
            _picture_comment(4, _cover_png((10, 20, 30))),
            _picture_comment(3, front)[:40],
            _picture_comment(3, front),
        ],
    )
    fi = _indexer(tmp_path)

    metadata = fi._extract_metadata(LOCAL, str(path))

    assert metadata["title"] == "Ladder"
    assert metadata["album_art"] == front
    assert "metadata_block_picture" not in metadata["raw_tags"]
    assert fi._embedded_art(LOCAL, str(path)) == front


def test_an_empty_picture_is_no_cover(tmp_path):
    """Zero bytes would be written off as a broken image instead of giving
    way to the next picture, or to the folder's cover."""
    front = _cover_png((90, 0, 160))
    path = _vorbis(tmp_path / "ladder.ogg", pictures=[_picture_comment(3, b"")])
    audio = OggVorbis(path)
    audio["coverart"] = ""
    audio.save()
    fi = _indexer(tmp_path)

    assert "album_art" not in fi._extract_metadata(LOCAL, str(path))

    audio["coverart"] = ["", base64.b64encode(front).decode("ascii")]
    audio.save()

    assert fi._extract_metadata(LOCAL, str(path))["album_art"] == front


@pytest.mark.parametrize("stream", [_opus, _ogg_flac], ids=["opus", "flac"])
def test_another_codec_in_ogg_stays_out_with_a_warning(tmp_path, caplog, stream):
    path = tmp_path / "track.ogg"
    path.write_bytes(stream())

    with caplog.at_level(logging.DEBUG, logger="indexer"):
        assert _indexer(tmp_path)._extract_metadata(LOCAL, str(path)) is None

    [record] = [r for r in caplog.records if r.name == "indexer"]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert str(path) in record.getMessage()
    assert "Vorbis" in record.getMessage()


def test_a_vorbis_stream_after_another_streams_first_page_is_found(tmp_path):
    """Multiplexed Ogg puts every stream's first page up front, in any order."""
    vorbis = _vorbis(tmp_path / "ladder.ogg").read_bytes()
    other = OggPage()
    other.serial = 99
    other.first = True
    other.packets = [b"\x80theora"]

    assert starts_vorbis_stream(io.BytesIO(other.write() + vorbis))


def test_a_damaged_vorbis_file_is_not_blamed_on_its_codec(tmp_path, caplog):
    path = _vorbis(tmp_path / "ladder.ogg")
    path.write_bytes(path.read_bytes()[:100])

    with caplog.at_level(logging.DEBUG, logger="indexer"):
        assert _indexer(tmp_path)._extract_metadata(LOCAL, str(path)) is None

    [record] = [r for r in caplog.records if r.name == "indexer"]
    assert record.levelno == logging.ERROR
    assert "no Vorbis stream" not in record.getMessage()


def test_the_legacy_coverart_comment_is_a_cover_too(tmp_path):
    front = _cover_png((0, 90, 200))
    path = _vorbis(tmp_path / "ladder.ogg")
    audio = OggVorbis(path)
    audio["coverart"] = base64.b64encode(front).decode("ascii")
    audio["coverartmime"] = "image/png"
    audio.save()

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["album_art"] == front
    assert "coverart" not in metadata["raw_tags"]


def test_a_single_stream_is_timed_as_mutagen_times_it(tmp_path):
    path = _vorbis(tmp_path / "ladder.ogg")

    with open(path, "rb") as audio:
        assert vorbis_length(audio) == OggVorbis(path).info.length


def test_a_chained_file_is_timed_over_every_link(tmp_path):
    """mutagen stops at the end of the first link; the renderer plays on."""
    link = _vorbis(tmp_path / "ladder.ogg").read_bytes()
    path = tmp_path / "rip.ogg"
    path.write_bytes(b"".join(_reserialed(link, serial) for serial in (1, 2, 3)))

    metadata = _indexer(tmp_path)._extract_metadata(LOCAL, str(path))

    assert metadata["duration"] == 18
    assert metadata["title"] == "Ladder"


def test_a_link_recorded_mid_stream_is_timed_from_where_it_starts(tmp_path):
    """The renderer plays such a link for only as long as was recorded;
    mutagen times it from position 0, as if from the start of the stream."""
    ladder = LADDER.read_bytes()
    first = _reserialed(_recorded_mid_stream(ladder, 3 * LADDER_RATE), 1)
    assert OggVorbis(io.BytesIO(first)).info.length == 9
    path = tmp_path / "rip.ogg"
    path.write_bytes(first + _reserialed(ladder, 2))

    with open(path, "rb") as audio:
        length = vorbis_length(audio)

    assert length == pytest.approx(
        2 * LADDER_FRAMES / LADDER_RATE, abs=1 / LADDER_RATE
    )
    assert _indexer(tmp_path)._extract_metadata(LOCAL, str(path))["duration"] == 12


def test_a_link_whose_setup_header_cannot_be_read_is_timed_from_0():
    data = _recorded_mid_stream(LADDER.read_bytes(), 3 * LADDER_RATE)

    assert vorbis_length(io.BytesIO(_with_setup_body_zeroed(data))) == 9


def test_a_cover_is_not_read_again_to_time_the_file(tmp_path):
    """Its tags were read from the comment header already, and a share
    would otherwise send every cover twice."""
    cover = bytes(range(256)) * 2400
    path = _vorbis(tmp_path / "ladder.ogg", pictures=[_picture_comment(3, cover)])
    audio = _CountingFile(_recorded_mid_stream(path.read_bytes(), LADDER_RATE))

    assert vorbis_length(audio) == pytest.approx(6, abs=1 / LADDER_RATE)
    assert audio.read_bytes < len(cover) // 4


def _long_link():
    """A link recorded mid-stream that runs past a read window, so that its
    last page is a read of its own."""
    filler = [_page([bytes(8000)]) for _ in range(4)]
    pages = [_page([SHORT, LONG], 10000), *filler, _page([SHORT], 40000)]
    return _written(_link_pages([0, 1], pages))


LONG_LINK_SECONDS = (40000 - 10000 + _decoded(256, 2048)) / 22050


def test_the_setup_and_first_audio_pages_come_with_the_opening_ones():
    """Each read is a round trip to a share. Past the last page, the two
    reads that find how the link opens hold its setup header and first audio
    page as well."""
    audio = _CountingFile(_long_link())

    assert vorbis_length(audio) == LONG_LINK_SECONDS
    assert audio.reads == 3


def test_where_each_links_audio_starts_costs_no_read_of_its_own(monkeypatch):
    """It is found ahead of the bisection for where the link ends, which
    would otherwise have the opening pages read again."""
    link = _long_link()
    data = b"".join(_reserialed(link, serial) for serial in (1, 2, 3))
    timed = _CountingFile(data)

    assert vorbis_length(timed) == pytest.approx(3 * LONG_LINK_SECONDS)

    monkeypatch.setattr(
        "kalinka_plugin_localfiles.indexer.ogg._audio_start", lambda *_: 0
    )
    untimed = _CountingFile(data)
    vorbis_length(untimed)

    assert timed.reads == untimed.reads


def test_the_mode_table_is_read_from_the_end_of_the_setup_header():
    """libvorbis, which encoded the renderer's fixture, writes one mode for
    short blocks and one for long."""
    assert _mode_blockflags(_setup_header(LADDER.read_bytes())) == [0, 1]


@pytest.mark.parametrize("flags", [[1], [1, 0, 1], [0, 1] * 32], ids=["1", "3", "64"])
def test_a_mode_table_of_any_size_is_read(flags):
    assert _mode_blockflags(_setup_with_modes(flags)) == flags


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        b"\x05vorbis" + bytes(64),
        b"\x03vorbis" + _setup_with_modes([0, 1])[7:],
    ],
    ids=["empty", "no-table", "comment-header"],
)
def test_a_packet_with_no_mode_table_has_no_modes(packet):
    assert _mode_blockflags(packet) is None


def test_a_packets_mode_is_read_as_libvorbisfile_reads_it():
    """With three modes libvorbis reads the mode in one bit where the
    decoder reads two, so 0x04 is mode 0, a short block, not mode 2."""
    pages = _link_pages(
        [0, 1, 1],
        [_page([b"\x04", b"\x04", LONG], 10000), _page([SHORT], 40000)],
    )
    start = 10000 - _decoded(256, 256, 2048)

    assert vorbis_length(io.BytesIO(_written(pages))) == (40000 - start) / 22050


def test_a_page_that_fails_its_checksum_loses_its_packets_as_in_libogg():
    """libogg passes over the page, and at the gap in the page numbers drops
    the packet it began; the block before the gap still counts."""
    pages = _link_pages(
        [0, 1],
        [
            _page([SHORT, SHORT]),
            _page([LONG, LONG + bytes(254)], complete=False),
            _page([bytes(10), SHORT], 10000, continued=True),
            _page([SHORT], 40000),
        ],
    )
    start = 10000 - _decoded(256, 256, 2048, 2048, 256)
    start_past_the_gap = 10000 - _decoded(256, 256, 256)

    whole = _written(pages)
    damaged = _written(pages, damaged=3)

    assert vorbis_length(io.BytesIO(whole)) == (40000 - start) / 22050
    assert vorbis_length(io.BytesIO(damaged)) == (40000 - start_past_the_gap) / 22050


def test_bytes_between_pages_are_passed_over():
    data = _recorded_mid_stream(LADDER.read_bytes(), 3 * LADDER_RATE)
    first_audio = next(page.offset for page in _pages(data) if page.position > 0)
    data = data[:first_audio] + b"not a page" * 100 + data[first_audio:]

    assert vorbis_length(io.BytesIO(data)) == pytest.approx(6, abs=1 / LADDER_RATE)


def test_each_link_is_timed_at_its_own_rate_from_a_few_small_reads():
    links = [(1, 44100, 1500), (2, 22050, 1000), (3, 48000, 1200)]
    audio = _CountingFile(b"".join(_timed_link(*link) for link in links))

    assert vorbis_length(audio) == pytest.approx(150 + 100 + 120)
    assert audio.read_bytes < len(audio.getvalue()) // 10


class _ShortReadFile(io.BytesIO):
    """A raw share handle, which answers at most one SMB READ per call."""

    def read(self, size=-1):
        return super().read(min(size, 4096) if size >= 0 else 4096)


def test_a_chain_is_timed_through_reads_that_come_back_short():
    links = [(1, 44100, 1500), (2, 22050, 1000), (3, 48000, 1200)]
    audio = _ShortReadFile(b"".join(_timed_link(*link) for link in links))

    assert vorbis_length(audio) == pytest.approx(150 + 100 + 120)


def test_an_ogg_file_is_read_without_read_ahead(tmp_path):
    """Its pages and a chain's bisection are small, scattered reads, and
    read-ahead turns each into 1 MiB from a share."""
    path = _vorbis(tmp_path / "ladder.ogg", pictures=[_picture_comment(3, b"x")])
    opens = []

    def open_(file_path, *, read_ahead=True):
        opens.append(read_ahead)
        return open(file_path, "rb")

    storage = SimpleNamespace(open=open_, listdir=lambda _: [])
    fi = _indexer(tmp_path)

    assert fi._extract_metadata(storage, str(path))["duration"] == 6
    assert fi._embedded_art(storage, str(path)) == b"x"
    assert opens == [False, False]


class _HeadOnlyFile(io.BytesIO):
    """A file on a share that answers for its first half only."""

    def read(self, size=-1):
        stop = len(self.getvalue()) if size < 0 else self.tell() + size
        if stop > len(self.getvalue()) // 2:
            raise OSError("host is down")
        return super().read(size)


def test_a_cover_is_read_without_timing_the_file(tmp_path):
    """Timing reads back from the end of every link; the cover sits in the
    opening pages."""
    front = _cover_png((120, 0, 60))
    data = _vorbis(
        tmp_path / "ladder.ogg", pictures=[_picture_comment(3, front)]
    ).read_bytes()
    storage = SimpleNamespace(open=lambda _, **__: _HeadOnlyFile(data))

    assert _indexer(tmp_path)._embedded_art(storage, "smb://nas/ladder.ogg") == front


class _DroppingFile(io.BytesIO):
    """A file on a share that stops answering after the first page header."""

    def read(self, size=-1):
        if self.tell() > 0:
            raise OSError("host is down")
        return super().read(size)


def test_a_share_that_drops_mid_read_is_not_taken_for_another_codec(tmp_path):
    """mutagen reports the failed read as a missing Vorbis stream, which
    would otherwise set a file that is fine aside until it changes."""
    data = _vorbis(tmp_path / "ladder.ogg").read_bytes()
    storage = SimpleNamespace(open=lambda _, **__: _DroppingFile(data))

    with pytest.raises(OSError, match="host is down"):
        _indexer(tmp_path)._extract_metadata(storage, "smb://nas/music/ladder.ogg")


@pytest.mark.asyncio
async def test_an_indexed_ogg_track_is_served_as_audio_ogg(tmp_path, monkeypatch):
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    music = tmp_path / "music"
    music.mkdir()
    path = _vorbis(music / "03 Ladder.ogg")
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

    assert info.mime_type == "audio/ogg"
    assert info.local_path == str(path)
    assert (await track.source_retriever()).format == "audio/ogg"
