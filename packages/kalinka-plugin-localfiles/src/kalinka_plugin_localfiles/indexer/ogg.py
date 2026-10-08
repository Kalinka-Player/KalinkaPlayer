"""Reading an Ogg file as the Ogg Vorbis the renderer plays.

A chained file, several streams played one after another as a radio rip is,
plays through every link in the renderer. mutagen times only the first link,
and finds its end by reading it page by page whenever another link follows.
The links are found instead the way libvorbisfile finds them: by bisecting
for where each one's pages stop, then reading back from there to its last
timed page. Each read is a small span, never the file.

Each link is timed from where its audio starts, as libvorbisfile times it,
since a link recorded from the middle of a stream, as the first of a radio
rip often is, does not start at position 0.
"""

import io
import itertools
import os
import struct
from typing import (
    BinaryIO,
    Callable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
)

from mutagen.ogg import OggPage
from mutagen.ogg import error as OggError
from mutagen.oggvorbis import OggVorbis, OggVorbisHeaderError, OggVorbisInfo

#: Read at a time: a few typical pages, so a bisection step is one small read.
_WINDOW = 16 * 1024
#: The largest page Ogg allows: a header, 255 lacing values, 255 full segments.
_MAX_PAGE = 27 + 255 + 255 * 255
_PAGE_HEADER = struct.Struct("<4sBBqIIIB")
_VORBIS_ID = b"\x01vorbis"
_SETUP_ID = b"\x05vorbis"
#: A mode in the setup header: block flag, window type, transform type, mapping.
_MODE_BITS = 1 + 16 + 16 + 8


class NoVorbisStream(Exception):
    """An Ogg file none of whose streams is Vorbis: Opus or FLAC, say."""


class _Page(NamedTuple):
    offset: int
    size: int
    page: OggPage


class _PageHeader(NamedTuple):
    """A page as its header describes it, its data unread."""

    offset: int
    first: bool
    serial: int
    lacing: bytes

    @property
    def end(self) -> int:
        return self.offset + _PAGE_HEADER.size + len(self.lacing) + sum(self.lacing)


class _Link(NamedTuple):
    """How one link of a chain opens."""

    serials: Set[int]
    vorbis: Optional[int]
    sample_rate: int
    #: Samples in a short and in a long block, from the identification header.
    blocksizes: Optional[Tuple[int, int]]
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

    @note A link whose setup header cannot be read is timed from position
        0, as mutagen times a whole file.
    """
    audio.seek(0, os.SEEK_END)
    end = audio.tell()
    spans = _Spans(audio)
    last = _last_page(spans, 0, end, lambda page: True)
    length, start = None, 0
    while last is not None and start < end:
        link = _link_at(spans, start, end)
        if not link.serials:
            break
        # Ahead of the bisection for its end, while its opening span is kept.
        begins = _audio_start(spans, link, end)
        if last.serial in link.serials:
            link_end = end
        else:
            link_end = _link_end(spans, link.serials, link.body, end)
        granule = _last_granule(
            spans, link, link_end, last if link_end == end else None
        )
        if granule is not None:
            played = max(0, granule - begins)
            length = (length or 0.0) + played / link.sample_rate
        start = link_end
    return length


def _mode_blockflags(setup: bytes) -> Optional[List[int]]:
    """Each mode's block flag, 0 for a short block and 1 for a long one, from
    a Vorbis setup header, or None when no mode table ends it.

    The table is read back from the end of the packet, as ffmpeg's
    vorbis_parser reads it, since reading forward to it means decoding every
    codebook, floor and residue first. From the framing bit back, each mode
    is an 8-bit mapping below 64, two 16-bit types that are 0 and the block
    flag; the count wanted is the longest run the 6 bits before it agree with.
    """
    if not setup.startswith(_SETUP_ID):
        return None
    bits = int.from_bytes(setup, "little")
    at = bits.bit_length() - 1
    flags: List[int] = []
    found = None
    while at - _MODE_BITS >= len(_SETUP_ID) * 8 and len(flags) < 64:
        mode = (bits >> (at - _MODE_BITS)) & ((1 << _MODE_BITS) - 1)
        if mode >> 33 > 63 or (mode >> 1) & 0xFFFFFFFF:
            break
        flags.append(mode & 1)
        at -= _MODE_BITS
        if (bits >> (at - 6)) & 0x3F == len(flags) - 1:
            found = flags[::-1]
    return found


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


class _Spans:
    """A file read a span at a time, each read a round trip to a share.

    The last span is kept, since the one that shows how a link opens
    usually holds its setup header and first audio page as well.
    """

    def __init__(self, audio: BinaryIO):
        self._audio = audio
        self._kept_at, self._kept = 0, b""

    def read(self, start: int, stop: int) -> bytes:
        if self._kept_at <= start and stop <= self._kept_at + len(self._kept):
            return self._kept[start - self._kept_at : stop - self._kept_at]
        # A raw share handle returns at most one SMB READ's worth per call.
        self._audio.seek(start)
        chunks, wanted = [], stop - start
        while wanted > 0:
            chunk = self._audio.read(wanted)
            if not chunk:
                break
            chunks.append(chunk)
            wanted -= len(chunk)
        self._kept_at, self._kept = start, b"".join(chunks)
        return self._kept


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


def _first_page(audio: _Spans, offset: int, end: int) -> Optional[_Page]:
    """The first page that starts at or after ``offset``."""
    return next(_pages_from(audio, offset, end), None)


def _pages_from(audio: _Spans, offset: int, end: int) -> Iterator[_Page]:
    """Every whole, intact page from ``offset`` on, read a window at a time.

    Bytes that are no page, or a page that fails its checksum, are passed
    over, as libogg passes over them.
    """
    while offset < end:
        resume = None
        for width in (_WINDOW, _WINDOW + _MAX_PAGE):
            stop = min(end, offset + width)
            for found in _pages_in(audio.read(offset, stop), offset):
                yield found
                resume = found.offset + found.size
            if resume is not None or stop == end:
                break
        if resume is not None:
            offset = resume
        elif stop < end:
            offset += _WINDOW
        else:
            return


def _last_page(
    audio: _Spans, start: int, end: int, wanted: Callable[[OggPage], bool]
) -> Optional[OggPage]:
    """The last page in [start, end) that ``wanted`` accepts, read back from
    ``end``, which must fall between two pages, a growing span at a time."""
    width = _WINDOW
    while True:
        begin = max(start, end - width)
        pages = [
            found.page
            for found in _pages_in(audio.read(begin, end), begin)
            if wanted(found.page)
        ]
        if pages:
            return pages[-1]
        if begin == start:
            return None
        width *= 4


def _link_at(audio: _Spans, start: int, end: int) -> _Link:
    serials: Set[int] = set()
    vorbis, sample_rate, blocksizes, body = None, 0, None, start
    found = _first_page(audio, start, end)
    while found is not None and found.page.first:
        serials.add(found.page.serial)
        packet = found.page.packets[0] if found.page.packets else b""
        if vorbis is None and packet.startswith(_VORBIS_ID) and len(packet) >= 16:
            vorbis = found.page.serial
            sample_rate = struct.unpack("<I", packet[12:16])[0]
            if len(packet) > 28:
                blocksizes = (1 << (packet[28] & 0x0F), 1 << (packet[28] >> 4))
        body = found.offset + found.size
        found = _first_page(audio, body, end)
    return _Link(serials, vorbis, sample_rate, blocksizes, body)


def _link_end(audio: _Spans, serials: Set[int], searched: int, end: int) -> int:
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
    audio: _Spans, link: _Link, end: int, last: Optional[OggPage] = None
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


def _audio_start(audio: _Spans, link: _Link, end: int) -> int:
    """The position at which a link's audio starts, or 0 when its setup
    header cannot be read.

    Worked out as libvorbisfile's _initial_pcmoffset does: the position of
    the first timed page after the headers, less what the packets up to its
    end decode to, which is (previous block + this block) / 4 for each one
    after the first.
    """
    if link.blocksizes is None:
        return 0
    pages = _vorbis_packets(audio, link, end)
    headers = next((packets for packets, _ in pages if packets), [b""])
    modes = _mode_blockflags(headers[0])
    if modes is None:
        return 0
    decoded, previous = 0, None
    # Audio on the setup header's page counts towards the next page's position.
    for packets, position in itertools.chain([(headers[1:], -1)], pages):
        for packet in packets:
            size = _block_size(packet, modes, link.blocksizes)
            if size is None:
                continue
            if previous is not None:
                decoded += (previous + size) >> 2
            previous = size
        if position != -1:
            return max(0, position - decoded)
    return 0


def _block_size(
    packet: bytes, modes: List[int], blocksizes: Tuple[int, int]
) -> Optional[int]:
    """Samples in the block an audio packet holds, None for any other packet.

    The mode is read in floor(log2(modes)) bits, as libvorbis's
    vorbis_packet_blocksize reads it for libvorbisfile, where the decoder
    reads ilog(modes - 1). The two differ only for 3, 5, 6 or 7 modes, which
    libvorbis never writes.
    """
    if not packet or packet[0] & 1:
        return None
    mode = (packet[0] >> 1) & ((1 << (len(modes).bit_length() - 1)) - 1)
    return blocksizes[modes[mode]]


def _vorbis_packets(
    audio: _Spans, link: _Link, end: int
) -> Iterator[Tuple[List[bytes], int]]:
    """A link's Vorbis packets after its comment header, as each page
    completes them, with that page's position.

    Packets are put together as libogg puts them together: a page lost to a
    bad checksum, seen as a gap in the page numbers, takes with it the packet
    it held part of and the rest of that packet on the next page.
    """
    offset, in_comment = _past_comment(audio, link, end)
    parts: Optional[List[bytes]] = [] if in_comment else None
    sequence = None
    for found in _pages_from(audio, offset, end):
        page = found.page
        if page.first:
            return
        if page.serial != link.vorbis:
            continue
        if sequence is not None and page.sequence != sequence:
            parts = None
        sequence = page.sequence + 1
        pieces = page.packets[1:] if parts is None and page.continued else page.packets
        packets = []
        for n, piece in enumerate(pieces):
            parts = parts if parts is not None else []
            parts.append(piece)
            if n < len(pieces) - 1 or page.complete:
                if not in_comment:
                    packets.append(b"".join(parts))
                in_comment, parts = False, None
        yield packets, page.position


def _past_comment(audio: _Spans, link: _Link, end: int) -> Tuple[int, bool]:
    """Where the first page after a link's opening ones that holds more than
    its comment header starts, and whether the comment is unfinished there.

    The pages before it are stepped over by their headers alone: the tags
    were read from them already, and pictures swell them to megabytes.
    """
    offset, in_comment = link.body, True
    for header in _page_headers(audio, link.body, end):
        if header.first:
            break
        if header.serial == link.vorbis:
            if not in_comment or any(lace < 255 for lace in header.lacing[:-1]):
                break
            in_comment = not header.lacing or header.lacing[-1] == 255
        offset = header.end
    return offset, in_comment


def _page_headers(audio: _Spans, offset: int, end: int) -> Iterator[_PageHeader]:
    """The headers of the pages from ``offset`` on, for as long as each page
    follows straight on from the one before."""
    while offset < end:
        data = audio.read(offset, min(end, offset + _PAGE_HEADER.size + 255))
        if len(data) < _PAGE_HEADER.size:
            return
        capture, version, flags, _, serial, _, _, segments = (
            _PAGE_HEADER.unpack_from(data)
        )
        lacing = data[_PAGE_HEADER.size : _PAGE_HEADER.size + segments]
        if capture != b"OggS" or version != 0 or len(lacing) < segments:
            return
        header = _PageHeader(offset, bool(flags & 0x02), serial, lacing)
        yield header
        offset = header.end
