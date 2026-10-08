"""Reading an Ogg file as the Ogg Vorbis the renderer plays.

A chained file, several streams played one after another as a radio rip is,
plays through every link in the renderer. mutagen times only the first link,
and finds its end by reading it page by page whenever another link follows.
The links are found instead the way libvorbisfile finds them: by bisecting
for where each one's pages stop, then reading back from there to its last
timed page. Each read is a small span, never the file.
"""

import io
import os
import struct
from typing import BinaryIO, Callable, Iterator, NamedTuple, Optional, Set

from mutagen.ogg import OggPage
from mutagen.ogg import error as OggError
from mutagen.oggvorbis import OggVorbis, OggVorbisHeaderError, OggVorbisInfo

#: Read at a time: a few typical pages, so a bisection step is one small read.
_WINDOW = 16 * 1024
#: The largest page Ogg allows: a header, 255 lacing values, 255 full segments.
_MAX_PAGE = 27 + 255 + 255 * 255
_VORBIS_ID = b"\x01vorbis"


class NoVorbisStream(Exception):
    """An Ogg file none of whose streams is Vorbis: Opus or FLAC, say."""


class _Page(NamedTuple):
    offset: int
    size: int
    page: OggPage


class _Link(NamedTuple):
    """How one link of a chain opens."""

    serials: Set[int]
    vorbis: Optional[int]
    sample_rate: int
    #: Where the pages after its opening ones start.
    body: int


def starts_vorbis_stream(audio: BinaryIO) -> bool:
    """Whether one of an Ogg file's streams is Vorbis, told from the opening
    pages alone.

    Every stream's first page precedes all other pages, so an Opus or FLAC
    file is turned away without being read to the end, which is what mutagen
    does while it looks for a Vorbis header.
    """
    try:
        page = OggPage(audio)
        while page.first:
            if page.packets and page.packets[0].startswith(_VORBIS_ID):
                return True
            page = OggPage(audio)
    except EOFError:
        pass
    return False


def vorbis_length(audio: BinaryIO) -> Optional[float]:
    """Seconds of Vorbis audio in an Ogg file, summed over the links of a
    chain, or None when no link has a timed Vorbis page.

    @note Each link is timed from its last granule position, as mutagen
        times a whole file, so a link recorded from the middle of a stream
        counts the audio before the recording began too.
    """
    audio.seek(0, os.SEEK_END)
    end = audio.tell()
    last = _last_page(audio, 0, end, lambda page: True)
    length, start = None, 0
    while last is not None and start < end:
        link = _link_at(audio, start, end)
        if not link.serials:
            break
        if last.serial in link.serials:
            link_end = end
        else:
            link_end = _link_end(audio, link.serials, link.body, end)
        granule = _last_granule(
            audio, link, link_end, last if link_end == end else None
        )
        if granule is not None:
            length = (length or 0.0) + granule / link.sample_rate
        start = link_end
    return length


class _ChainVorbisInfo(OggVorbisInfo):
    def _post_tags(self, fileobj):
        length = vorbis_length(fileobj)
        if length is None:
            raise OggVorbisHeaderError("no Vorbis page gives a position")
        self.length = length


class _ChainedOggVorbis(OggVorbis):
    """mutagen's Ogg Vorbis file, timed over every link of a chain.

    @note ``_Info`` is where each of mutagen's Ogg formats says how it is
        timed; the chained-file tests fail should that ever change.
    """

    _Info = _ChainVorbisInfo


class _UntimedVorbisInfo(OggVorbisInfo):
    def _post_tags(self, fileobj):
        pass


class _UntimedOggVorbis(OggVorbis):
    _Info = _UntimedVorbisInfo


def open_ogg_vorbis(audio: BinaryIO) -> OggVorbis:
    """Parse an open Ogg Vorbis file with mutagen, timed as the renderer
    plays it.

    @raise NoVorbisStream If none of its streams is Vorbis.
    """
    return _open(audio, _ChainedOggVorbis)


def open_ogg_vorbis_tags(audio: BinaryIO) -> OggVorbis:
    """Parse just the opening pages of an open Ogg Vorbis file, which hold
    its tags, leaving its length at 0: timing reads back from the end of
    every link.

    @raise NoVorbisStream If none of its streams is Vorbis.
    """
    return _open(audio, _UntimedOggVorbis)


def _open(audio: BinaryIO, kind: Callable[[BinaryIO], OggVorbis]) -> OggVorbis:
    if not starts_vorbis_stream(audio):
        raise NoVorbisStream()
    audio.seek(0)
    return kind(audio)


def _read(audio: BinaryIO, start: int, stop: int) -> bytes:
    # A raw share handle returns at most one SMB READ's worth per call.
    audio.seek(start)
    chunks, wanted = [], stop - start
    while wanted > 0:
        chunk = audio.read(wanted)
        if not chunk:
            break
        chunks.append(chunk)
        wanted -= len(chunk)
    return b"".join(chunks)


def _pages_in(data: bytes, base: int) -> Iterator[_Page]:
    """Every whole, intact page in ``data``, which was read from ``base``."""
    stream = io.BytesIO(data)
    at = data.find(b"OggS")
    while at >= 0:
        stream.seek(at)
        try:
            page = OggPage(stream)
        except (OggError, EOFError):
            page = None
        size = stream.tell() - at
        # The checksum as well, since "OggS" can turn up inside the audio.
        if page is not None and page.write() == data[at : at + size]:
            yield _Page(base + at, size, page)
            at = data.find(b"OggS", at + size)
        else:
            at = data.find(b"OggS", at + 1)


def _first_page(audio: BinaryIO, offset: int, end: int) -> Optional[_Page]:
    """The first page that starts at or after ``offset``."""
    while offset < end:
        for width in (_WINDOW, _WINDOW + _MAX_PAGE):
            stop = min(end, offset + width)
            found = next(_pages_in(_read(audio, offset, stop), offset), None)
            if found is not None or stop == end:
                return found
        offset += _WINDOW
    return None


def _last_page(
    audio: BinaryIO, start: int, end: int, wanted: Callable[[OggPage], bool]
) -> Optional[OggPage]:
    """The last page in [start, end) that ``wanted`` accepts, read back from
    ``end``, which must fall between two pages, a growing span at a time."""
    width = _WINDOW
    while True:
        begin = max(start, end - width)
        pages = [
            found.page
            for found in _pages_in(_read(audio, begin, end), begin)
            if wanted(found.page)
        ]
        if pages:
            return pages[-1]
        if begin == start:
            return None
        width *= 4


def _link_at(audio: BinaryIO, start: int, end: int) -> _Link:
    serials: Set[int] = set()
    vorbis, sample_rate, body = None, 0, start
    found = _first_page(audio, start, end)
    while found is not None and found.page.first:
        serials.add(found.page.serial)
        packet = found.page.packets[0] if found.page.packets else b""
        if vorbis is None and packet.startswith(_VORBIS_ID) and len(packet) >= 16:
            vorbis = found.page.serial
            sample_rate = struct.unpack("<I", packet[12:16])[0]
        body = found.offset + found.size
        found = _first_page(audio, body, end)
    return _Link(serials, vorbis, sample_rate, body)


def _link_end(audio: BinaryIO, serials: Set[int], searched: int, end: int) -> int:
    """Where the link of ``serials`` stops: the first page after
    ``searched`` that belongs to another link, or ``end``.

    Bisects as libvorbisfile does, which holds because a link's pages all
    come before the next link's.
    """
    boundary, unsearched = end, end
    while searched < unsearched:
        if unsearched - searched < _WINDOW:
            bisect = searched
        else:
            bisect = (searched + unsearched) // 2
        found = _first_page(audio, bisect, end)
        if found is None or found.page.serial not in serials:
            unsearched = bisect
            if found is not None:
                boundary = found.offset
        else:
            searched = found.offset + found.size
    return boundary


def _last_granule(
    audio: BinaryIO, link: _Link, end: int, last: Optional[OggPage] = None
) -> Optional[int]:
    """``last`` is the last page before ``end``, if already read."""
    if link.vorbis is None or not link.sample_rate:
        return None

    def timed(page: OggPage) -> bool:
        return page.serial == link.vorbis and page.position != -1

    if last is not None and timed(last):
        return last.position
    page = _last_page(audio, link.body, end, timed)
    return None if page is None else page.position
