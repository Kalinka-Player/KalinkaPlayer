"""Reading an MP4 file as the AAC or FLAC audio the renderer plays from one.

mutagen describes the first audio track and reads the tags. Two things it
leaves out are read here. A FLAC track's rate, channels and precision come
from its STREAMINFO, since the sample entry has no room for a rate above
65535 Hz. A fragmented file is timed over its fragments, where mutagen
times only the samples the movie header lists, usually none.
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
_FLAC_DEPTHS = (16, 24, 32)
_FLAC_CHANNELS = 2
#: The stsd's version, flags and entry count, then its first entry's box
#: header and AudioSampleEntry fields, which its own boxes follow.
_SAMPLE_ENTRY_AT = 8
_SAMPLE_ENTRY_BOXES_AT = _SAMPLE_ENTRY_AT + 8 + 28
_STREAMINFO_SIZE = 34

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


class Mp4Audio(NamedTuple):
    """The audio track the renderer plays from an MP4 file, and its tags."""

    #: mutagen's account of the track, corrected for FLAC and fragments.
    info: MP4Info
    #: "aac" or "flac".
    codec: str
    #: The tags under Vorbis comment names, each a list of values; a
    #: freeform item under its own name, lower-cased.
    comments: Dict[str, List[str]]
    cover: Optional[bytes]


def open_mp4_audio(audio: BinaryIO) -> Mp4Audio:
    """Parse an open MP4 file whose first audio track the renderer plays.

    @raise UnplayableFile If it has no audio track, or the track is neither
        AAC-LC or HE-AAC, nor FLAC in mono or stereo at 16, 24 or 32 bits.
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
        _describe_flac(info, _flac_stream_info(trak, audio))
    else:
        raise UnplayableFile(
            f"its audio is {info.codec_description}, and the renderer plays "
            "only AAC-LC, HE-AAC and FLAC from MP4"
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


def _flac_stream_info(trak: Atom, audio: BinaryIO) -> StreamInfo:
    stsd = _read(trak[b"mdia", b"minf", b"stbl", b"stsd"], audio)
    entry_end = _SAMPLE_ENTRY_AT + struct.unpack_from(">I", stsd, _SAMPLE_ENTRY_AT)[0]
    for name, body in _boxes(stsd[_SAMPLE_ENTRY_BOXES_AT:entry_end]):
        # A full box, then metadata blocks, STREAMINFO (type 0) first.
        if name == b"dfLa" and len(body) >= 8 + _STREAMINFO_SIZE and not body[4] & 0x7F:
            return StreamInfo(body[8 : 8 + _STREAMINFO_SIZE])
    raise UnplayableFile("its FLAC track carries no STREAMINFO")


def _describe_flac(info: MP4Info, stream: StreamInfo) -> None:
    if stream.channels > _FLAC_CHANNELS:
        raise UnplayableFile(
            f"its FLAC audio has {stream.channels} channels, and the renderer "
            "plays FLAC from MP4 in mono or stereo only"
        )
    if stream.bits_per_sample not in _FLAC_DEPTHS:
        raise UnplayableFile(
            f"its FLAC audio is {stream.bits_per_sample}-bit, and the renderer "
            "plays FLAC from MP4 at 16, 24 or 32 bits only"
        )
    info.sample_rate = stream.sample_rate
    info.channels = stream.channels
    info.bits_per_sample = stream.bits_per_sample


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
