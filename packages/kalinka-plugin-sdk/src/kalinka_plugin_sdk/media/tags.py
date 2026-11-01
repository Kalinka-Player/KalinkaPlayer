"""Opening a music file's tags and finding the cover they carry.

One :class:`TagReader` per media type, so a format is added in one place
and everything that reads music — a library's indexer, a folder listing
that plays files straight from a drive — reads it the same way.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, List, Mapping, NamedTuple, Optional

from mutagen import MutagenError
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen.mp3 import MP3

from .adts import AdtsFile
from .formats import media_type_of
from .mp4 import open_mp4_audio
from .ogg import open_ogg_vorbis, open_ogg_vorbis_tags
from .wav import open_pcm_wave


class EmbeddedCover(NamedTuple):
    """A file's embedded cover and how it is typed: 3 (front cover) or 0
    (other, which is what many taggers write for the cover, and all an MP4
    or a legacy Vorbis COVERART can say)."""

    data: bytes
    picture_type: int


def id3_cover(audio_file) -> Optional[EmbeddedCover]:
    """The front cover from a file's ID3 tag, else a picture typed "other",
    or None.

    Chosen by the frame's picture type: its description is free text, so
    "Front Cover" and "" may both be the front, and "" may be the back.
    """
    id3 = audio_file.tags
    if id3 is None:
        return None
    frames = [frame for frame in id3.getall("APIC") if frame.data]
    for wanted in (PictureType.COVER_FRONT, PictureType.OTHER):
        for frame in frames:
            if frame.type == wanted:
                return EmbeddedCover(frame.data, int(wanted))
    return None


def front_cover(pictures: List[Picture]) -> Optional[EmbeddedCover]:
    """The front cover from FLAC picture blocks, or None.

    Takes the front cover, else a picture typed "other", which is what
    many taggers write for the cover. A back cover, booklet page or disc
    label is never taken: the album's folder or an online source has the
    real front.
    """
    for wanted in (PictureType.COVER_FRONT, PictureType.OTHER):
        for pic in pictures:
            if pic.type == wanted:
                return EmbeddedCover(pic.data, int(wanted))
    return None


def flac_cover(flac) -> Optional[EmbeddedCover]:
    """The front cover from a FLAC file's picture blocks, or None."""
    return front_cover(flac.pictures)


def vorbis_cover(vorbis) -> Optional[EmbeddedCover]:
    """The front cover from an Ogg Vorbis file's base64
    ``METADATA_BLOCK_PICTURE`` comments, or failing those the legacy
    ``COVERART`` one, or None.

    A comment that will not decode is passed over: it is one tag among
    many, and must not cost the track the rest.
    """
    pictures = []
    for encoded in vorbis.get("metadata_block_picture", []):
        try:
            pictures.append(Picture(base64.b64decode(encoded)))
        except (ValueError, MutagenError):
            continue
    cover = front_cover([p for p in pictures if p.data])
    if cover is not None:
        return cover
    for encoded in vorbis.get("coverart", []):
        try:
            data = base64.b64decode(encoded)
        except ValueError:
            continue
        if data:
            return EmbeddedCover(data, int(PictureType.OTHER))
    return None


def mp4_cover(mp4) -> Optional[EmbeddedCover]:
    """An MP4 file's first picture. MP4 has no picture types."""
    if mp4.cover is None:
        return None
    return EmbeddedCover(mp4.cover, int(PictureType.OTHER))


@dataclass(frozen=True)
class TagSummary:
    """What a listing shows of a track, read from its own tags."""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    year: Optional[int] = None
    duration_s: Optional[float] = None
    codec: Optional[str] = None
    has_cover: bool = False


_ID3_FIELDS = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "album_artist": "TPE2",
    "track": "TRCK",
    "disc": "TPOS",
    "date": "TDRC",
}
_VORBIS_FIELDS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "track": "tracknumber",
    "disc": "discnumber",
    "date": "date",
}
_LEADING_INT = re.compile(r"\s*(\d+)")
_YEAR = re.compile(r"(\d{4})")


def _number(value: Optional[str]) -> Optional[int]:
    match = _LEADING_INT.match(value or "")
    return int(match.group(1)) if match else None


def _year(value: Optional[str]) -> Optional[int]:
    match = _YEAR.search(value or "")
    return int(match.group(1)) if match else None


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _id3_values(parsed) -> Dict[str, Optional[str]]:
    tags = getattr(parsed, "tags", None)
    if tags is None:
        return {}
    values = {}
    for field, frame in _ID3_FIELDS.items():
        found = tags.get(frame)
        values[field] = _text(found.text[0]) if found is not None and found.text else None
    return values


def _vorbis_values(comments: Mapping[str, List[str]]) -> Dict[str, Optional[str]]:
    values = {}
    for field, name in _VORBIS_FIELDS.items():
        found = comments.get(name) or []
        values[field] = _text(found[0]) if found else None
    return values


@dataclass(frozen=True)
class TagReader:
    """How one kind of audio file is read, for its tags and its cover.

    @param read Parses an open file, raising when it is not this kind.
    @param cover The front cover in the parsed file, or None.
    @param values Title, artist and the rest in the parsed file, by the
        names :class:`TagSummary` uses, as text.
    @param codec The codec the parsed file holds.
    @param read_ahead_bytes The read-ahead to open the file with; 0 for a
        reader of small, scattered spans, None for the caller's default.
    @param read_tags Parses just enough of an open file for ``cover``, where
        ``read`` does more than that; None to use ``read``.
    """

    read: Callable[[BinaryIO], Any]
    cover: Callable[[Any], Optional[EmbeddedCover]]
    values: Callable[[Any], Dict[str, Optional[str]]]
    codec: Callable[[Any], Optional[str]]
    read_ahead_bytes: Optional[int] = None
    read_tags: Optional[Callable[[BinaryIO], Any]] = None


def _fixed(codec: str) -> Callable[[Any], str]:
    return lambda _parsed: codec


def _vorbis(parsed) -> Dict[str, Optional[str]]:
    return _vorbis_values(parsed.tags or {})


#: How each media type :func:`~.formats.media_type_of` names is read.
TAG_READERS: Dict[str, TagReader] = {
    "audio/mpeg": TagReader(MP3, id3_cover, _id3_values, _fixed("mp3")),
    "audio/flac": TagReader(FLAC, flac_cover, _vorbis, _fixed("flac")),
    "audio/wav": TagReader(open_pcm_wave, id3_cover, _id3_values, _fixed("pcm")),
    "audio/ogg": TagReader(
        open_ogg_vorbis,
        vorbis_cover,
        _vorbis,
        _fixed("vorbis"),
        read_ahead_bytes=0,
        read_tags=open_ogg_vorbis_tags,
    ),
    "audio/mp4": TagReader(
        open_mp4_audio,
        mp4_cover,
        lambda mp4: _vorbis_values(mp4.comments),
        lambda mp4: mp4.codec,
    ),
    "audio/aac": TagReader(AdtsFile, id3_cover, _id3_values, _fixed("aac")),
    "audio/x-dsf": TagReader(DSF, id3_cover, _id3_values, _fixed("dsd")),
    "audio/x-dff": TagReader(DSDIFF, id3_cover, _id3_values, _fixed("dsd")),
}


def reader_for(path: str) -> Optional[TagReader]:
    return TAG_READERS.get(media_type_of(path) or "")


def read_summary(path: str) -> Optional[TagSummary]:
    """A local file's tags as a listing shows them, or None when the file is
    not one of the readable formats or will not parse.

    @raise OSError If the file cannot be read at all.
    """
    reader = reader_for(path)
    if reader is None:
        return None
    with open(path, "rb") as audio:
        try:
            parsed = reader.read(audio)
            values = reader.values(parsed)
        except OSError:
            raise
        except Exception:  # noqa: BLE001 — a file from anywhere may be malformed any way
            return None
    info = getattr(parsed, "info", None)
    length = getattr(info, "length", None)
    return TagSummary(
        title=values.get("title"),
        artist=values.get("artist"),
        album=values.get("album"),
        album_artist=values.get("album_artist"),
        track_number=_number(values.get("track")),
        disc_number=_number(values.get("disc")),
        year=_year(values.get("date")),
        duration_s=float(length) if length else None,
        codec=reader.codec(parsed),
        has_cover=reader.cover(parsed) is not None,
    )


def read_cover(path: str) -> Optional[bytes]:
    """The front cover embedded in a local file, or None.

    @raise OSError If the file cannot be read at all.
    """
    reader = reader_for(path)
    if reader is None:
        return None
    with open(path, "rb") as audio:
        try:
            cover = reader.cover((reader.read_tags or reader.read)(audio))
        except OSError:
            raise
        except Exception:  # noqa: BLE001 — a file from anywhere may be malformed any way
            return None
    return cover.data if cover is not None else None


def title_from_filename(path: str) -> str:
    """A title for a file whose tags name none: its name, less the
    extension and a leading track number."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"^\s*\d{1,3}\s*[-._)]?\s+", "", stem) or stem
