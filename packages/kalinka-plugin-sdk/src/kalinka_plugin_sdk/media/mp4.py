"""Reading an MP4 file as the AAC, FLAC or ALAC audio the renderer plays
from one.

mutagen describes the first audio track and reads the tags. Two things it
leaves out are read here. A lossless track's rate, channels and precision
come from its FLAC STREAMINFO or ALAC configuration, since the sample entry
has no room for a rate above 65535 Hz. A fragmented file is timed over its
fragments, where mutagen times only the samples the movie header lists,
usually none.
"""

import struct
from typing import BinaryIO, Dict, Iterator, List, NamedTuple, Optional, Tuple

from mutagen.flac import StreamInfo
from mutagen.mp4 import (
    AtomDataType,
    MP4FreeForm,
    MP4Info,
    MP4NoTrackError,
    MP4StreamInfoError,
    MP4Tags,
)
from mutagen.mp4._atom import Atom, Atoms

from .unplayable import UnplayableFile

#: mutagen names HE-AAC v1 and v2 by the AAC-LC core they extend.
_AAC = "mp4a.40.2"
_FLAC = "fLaC"
_ALAC = "alac"
_LOSSLESS_DEPTHS = (16, 24, 32)
_LOSSLESS_CHANNELS = 2
_LOSSLESS_RATE = 768000
_ALAC_FRAMES = 65536
#: The stsd's version, flags and entry count, then its first entry's box
#: header and AudioSampleEntry fields, which its own boxes follow.
_SAMPLE_ENTRY_AT = 8
_SAMPLE_ENTRY_BOXES_AT = _SAMPLE_ENTRY_AT + 8 + 28
_STREAMINFO_SIZE = 34
#: The ALACSpecificConfig, after its box's version and flags.
_ALAC_CONFIG = struct.Struct(">IBBBBBBHIII")

_TFHD_BASE_DATA_OFFSET = 0x01
_TFHD_SAMPLE_DESCRIPTION = 0x02
_TFHD_DEFAULT_DURATION = 0x08
_TRUN_DATA_OFFSET = 0x01
_TRUN_FIRST_SAMPLE_FLAGS = 0x04
_TRUN_DURATIONS = 0x100
_TRUN_SAMPLE_FIELDS = 0xF00

#: MP4 metadata items under the Vorbis comment names the library reads.
_COMMENT_NAMES = {
    "\xa9nam": "title",
    "\xa9ART": "artist",
    "\xa9alb": "album",
    "aART": "albumartist",
    "\xa9day": "date",
    "\xa9gen": "genre",
    "\xa9wrt": "composer",
    "\xa9grp": "grouping",
    "\xa9cmt": "comment",
    "\xa9lyr": "lyrics",
    "\xa9too": "encoder",
    "cprt": "copyright",
    "desc": "description",
    "trkn": "tracknumber",
    "disk": "discnumber",
    "cpil": "compilation",
    "tmpo": "bpm",
    "soal": "albumsort",
    "soaa": "albumartistsort",
    "soar": "artistsort",
    "sonm": "titlesort",
    "soco": "composersort",
}
_FREEFORM = "----:"
_COVER = "covr"
_TEXT_ENCODINGS = {
    AtomDataType.IMPLICIT: "utf-8",
    AtomDataType.UTF8: "utf-8",
    AtomDataType.UTF16: "utf-16-be",
}


class _AlacConfig(NamedTuple):
    frames: int
    version: int
    bits: int
    mean_rate: int
    initial_mean: int
    rice_limit: int
    channels: int
    longest_run: int
    largest_frame: int
    bitrate: int
    rate: int


class Mp4Audio(NamedTuple):
    """The audio track the renderer plays from an MP4 file, and its tags."""

    #: mutagen's account of the track, corrected for lossless audio and
    #: fragments.
    info: MP4Info
    #: "aac", "flac" or "alac".
    codec: str
    #: The tags under Vorbis comment names, each a list of values; a
    #: freeform item under its own name, lower-cased.
    comments: Dict[str, List[str]]
    cover: Optional[bytes]


def open_mp4_audio(audio: BinaryIO) -> Mp4Audio:
    """Parse an open MP4 file whose first audio track the renderer plays.

    @raise UnplayableFile If it has no audio track, or the track is neither
        AAC-LC or HE-AAC, nor FLAC or ALAC in mono or stereo at 16, 24 or 32
        bits.
    """
    atoms = Atoms(audio)
    info = MP4Info()
    try:
        info.load(atoms, audio)
    except MP4NoTrackError:
        pass
    if not info.codec:
        raise UnplayableFile("it holds no audio track")
    trak = _audio_track(atoms, audio)
    if info.codec == _AAC:
        codec = "aac"
        # The sample entry's sample size means nothing for lossy audio.
        info.bits_per_sample = None
    elif info.codec == _FLAC:
        codec = "flac"
        stream = _flac_stream_info(trak, audio)
        _describe_lossless(
            info, "FLAC", stream.sample_rate, stream.channels, stream.bits_per_sample
        )
    elif info.codec == _ALAC:
        codec = "alac"
        _describe_lossless(info, "ALAC", *_alac_config(trak, audio))
    else:
        raise UnplayableFile(
            f"its audio is {info.codec_description}, and the renderer plays "
            "only AAC-LC, HE-AAC, FLAC and ALAC from MP4"
        )
    length = _fragmented_length(atoms, trak, audio)
    if length is not None:
        info.length = length
    tags = MP4Tags(atoms, audio) if _has_tags(atoms) else None
    return Mp4Audio(info, codec, _comments(tags), _front_cover(tags))


def _read(atom: Atom, audio: BinaryIO) -> bytes:
    ok, data = atom.read(audio)
    if not ok:
        raise MP4StreamInfoError(f"truncated {atom.name!r} atom")
    return data


def _boxes(data: bytes) -> Iterator[Tuple[bytes, bytes]]:
    """The type and payload of each box laid end to end in ``data``."""
    at = 0
    while at + 8 <= len(data):
        size, name = struct.unpack_from(">I4s", data, at)
        if size < 8:
            return
        yield name, data[at + 8 : at + size]
        at += size


def _audio_track(atoms: Atoms, audio: BinaryIO) -> Atom:
    """The track mutagen describes: the first whose handler is sound."""
    for trak in atoms[b"moov",].findall(b"trak"):
        if _read(trak[b"mdia", b"hdlr"], audio)[8:12] == b"soun":
            return trak
    raise MP4StreamInfoError("no sound track")


def _sample_entry_box(trak: Atom, audio: BinaryIO, name: bytes) -> Optional[bytes]:
    """The payload of the ``name`` box in the track's first sample entry."""
    stsd = _read(trak[b"mdia", b"minf", b"stbl", b"stsd"], audio)
    entry_end = _SAMPLE_ENTRY_AT + struct.unpack_from(">I", stsd, _SAMPLE_ENTRY_AT)[0]
    for child, body in _boxes(stsd[_SAMPLE_ENTRY_BOXES_AT:entry_end]):
        if child == name:
            return body
    return None


def _flac_stream_info(trak: Atom, audio: BinaryIO) -> StreamInfo:
    body = _sample_entry_box(trak, audio, b"dfLa")
    # A full box, then metadata blocks, STREAMINFO (type 0) first.
    if body is None or len(body) < 8 + _STREAMINFO_SIZE or body[4] & 0x7F:
        raise UnplayableFile("its FLAC track carries no STREAMINFO")
    return StreamInfo(body[8 : 8 + _STREAMINFO_SIZE])


def _alac_config(trak: Atom, audio: BinaryIO) -> Tuple[int, int, int]:
    """The rate, channels and depth an ALAC track's configuration states."""
    body = _sample_entry_box(trak, audio, b"alac")
    if body is None or len(body) < 4 + _ALAC_CONFIG.size or any(body[:4]):
        raise UnplayableFile("its ALAC track carries no configuration")
    config = _AlacConfig._make(_ALAC_CONFIG.unpack_from(body, 4))
    if config.version:
        raise UnplayableFile(
            f"its ALAC configuration is version {config.version}, which the "
            "renderer does not read"
        )
    if not 1 <= config.frames <= _ALAC_FRAMES:
        raise UnplayableFile(
            f"its ALAC frames hold {config.frames} samples, and the renderer "
            f"plays frames of 1 to {_ALAC_FRAMES}"
        )
    return config.rate, config.channels, config.bits


def _describe_lossless(
    info: MP4Info, name: str, rate: int, channels: int, bits: int
) -> None:
    if not 1 <= channels <= _LOSSLESS_CHANNELS:
        raise UnplayableFile(
            f"its {name} audio has {channels} channels, and the renderer "
            f"plays {name} from MP4 in mono or stereo only"
        )
    if bits not in _LOSSLESS_DEPTHS:
        raise UnplayableFile(
            f"its {name} audio is {bits}-bit, and the renderer plays {name} "
            "from MP4 at 16, 24 or 32 bits only"
        )
    if not 1 <= rate <= _LOSSLESS_RATE:
        raise UnplayableFile(
            f"its {name} audio runs at {rate} Hz, and the renderer plays "
            f"{name} from MP4 at up to {_LOSSLESS_RATE} Hz"
        )
    info.sample_rate = rate
    info.channels = channels
    info.bits_per_sample = bits


def _fragmented_length(atoms: Atoms, trak: Atom, audio: BinaryIO) -> Optional[float]:
    """Seconds of the track over the movie header's samples and every
    fragment's, or None for a file that is not fragmented."""
    try:
        mvex = atoms[b"moov", b"mvex"]
    except KeyError:
        return None
    track_id = _after_dates(_read(trak[b"tkhd",], audio))
    timescale = _after_dates(_read(trak[b"mdia", b"mdhd"], audio))
    default = 0
    for name, body in _boxes(_read(mvex, audio)):
        if name == b"trex" and struct.unpack_from(">I", body, 4)[0] == track_id:
            default = struct.unpack_from(">I", body, 12)[0]
    ticks = _listed_ticks(_read(trak[b"mdia", b"minf", b"stbl", b"stts"], audio))
    for moof in atoms.atoms:
        if moof.name != b"moof":
            continue
        for traf in moof.findall(b"traf"):
            ticks += _fragment_ticks(traf, audio, track_id, default)
    return ticks / timescale if timescale else 0.0


def _after_dates(data: bytes) -> int:
    """The field after a tkhd's or mdhd's creation and modification times:
    the track's ID, or its media's timescale."""
    return struct.unpack_from(">I", data, 20 if data[0] == 1 else 12)[0]


def _listed_ticks(stts: bytes) -> int:
    (count,) = struct.unpack_from(">I", stts, 4)
    return sum(
        samples * delta
        for samples, delta in struct.iter_unpack(">II", stts[8 : 8 + 8 * count])
    )


def _fragment_ticks(traf: Atom, audio: BinaryIO, track_id: int, default: int) -> int:
    tfhd = _read(traf[b"tfhd",], audio)
    flags = int.from_bytes(tfhd[1:4], "big")
    if struct.unpack_from(">I", tfhd, 4)[0] != track_id:
        return 0
    at = 8
    if flags & _TFHD_BASE_DATA_OFFSET:
        at += 8
    if flags & _TFHD_SAMPLE_DESCRIPTION:
        at += 4
    if flags & _TFHD_DEFAULT_DURATION:
        (default,) = struct.unpack_from(">I", tfhd, at)
    return sum(
        _run_ticks(_read(trun, audio), default) for trun in traf.findall(b"trun")
    )


def _run_ticks(trun: bytes, default: int) -> int:
    flags = int.from_bytes(trun[1:4], "big")
    (count,) = struct.unpack_from(">I", trun, 4)
    if not flags & _TRUN_DURATIONS:
        return count * default
    at = 8
    if flags & _TRUN_DATA_OFFSET:
        at += 4
    if flags & _TRUN_FIRST_SAMPLE_FLAGS:
        at += 4
    stride = 4 * bin(flags & _TRUN_SAMPLE_FIELDS).count("1")
    return sum(struct.unpack_from(">I", trun, at + n * stride)[0] for n in range(count))


def _has_tags(atoms: Atoms) -> bool:
    return [b"moov", b"udta", b"meta", b"ilst"] in atoms


def _comments(tags: Optional[MP4Tags]) -> Dict[str, List[str]]:
    comments: Dict[str, List[str]] = {}
    for key, values in (tags or {}).items():
        if key == _COVER:
            continue
        if key.startswith(_FREEFORM):
            name = key.split(":", 2)[-1].lower()
        else:
            name = _COMMENT_NAMES.get(key, key)
        texts = [
            text
            for text in map(_text, values if isinstance(values, list) else [values])
            if text is not None
        ]
        if texts:
            comments.setdefault(name, []).extend(texts)
    return comments


def _text(value) -> Optional[str]:
    """A tag value as text, or None for binary data."""
    if isinstance(value, MP4FreeForm):
        encoding = _TEXT_ENCODINGS.get(value.dataformat)
        return None if encoding is None else bytes(value).decode(encoding, "replace")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, tuple):
        number, total = value
        return f"{number}/{total}" if total else str(number)
    return str(value)


def _front_cover(tags: Optional[MP4Tags]) -> Optional[bytes]:
    """The first picture, which by convention is the front cover."""
    for cover in (tags or {}).get(_COVER, []):
        if cover:
            return bytes(cover)
    return None
