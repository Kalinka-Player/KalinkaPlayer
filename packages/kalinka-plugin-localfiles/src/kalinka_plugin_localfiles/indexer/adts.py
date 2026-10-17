"""Reading a raw AAC file as the ADTS stream the renderer plays.

mutagen times an ADTS stream but reads no tags from it, and finds its frames
by searching the opening bytes for anything shaped like a sync word, an MP3
frame included. The renderer starts decoding where the ID3v2 tag ends, so the
frames are checked there first.
"""

import os
from typing import BinaryIO, NamedTuple, Optional

from mutagen.aac import AACInfo
from mutagen.id3 import ID3FileType

from .unplayable import UnplayableFile

_HEADER_SIZE = 7
#: As many frames as mutagen needs to time the stream.
_FRAMES_CHECKED = 3
#: The profile field holds the object type less one; HE-AAC is signalled as LC.
_PROFILE_LC = 1
_SAMPLING_RATES = 13


class _Frame(NamedTuple):
    #: The header fields every frame of one stream repeats.
    fixed: int
    profile: int
    length: int


def _frame(header: bytes) -> Optional[_Frame]:
    if len(header) < _HEADER_SIZE:
        return None
    bits = int.from_bytes(header[:_HEADER_SIZE], "big")
    sync, layer = bits >> 44, (bits >> 41) & 0x3
    protection_absent = (bits >> 40) & 0x1
    sampling_rate = (bits >> 34) & 0xF
    length = (bits >> 13) & 0x1FFF
    if (
        sync != 0xFFF
        or layer != 0
        or sampling_rate >= _SAMPLING_RATES
        or length < (_HEADER_SIZE if protection_absent else _HEADER_SIZE + 2)
    ):
        return None
    return _Frame(bits >> 28, (bits >> 38) & 0x3, length)


def _require_adts(audio: BinaryIO, start: int) -> None:
    audio.seek(start)
    first = None
    for _ in range(_FRAMES_CHECKED):
        header = audio.read(_HEADER_SIZE)
        if not header and first is not None:
            break
        frame = _frame(header)
        if frame is None or (first is not None and frame.fixed != first.fixed):
            raise UnplayableFile("it is not an ADTS stream of AAC")
        first = first or frame
        audio.seek(frame.length - _HEADER_SIZE, os.SEEK_CUR)
    if first.profile != _PROFILE_LC:
        raise UnplayableFile(
            "its AAC is not AAC-LC or HE-AAC, the profiles the renderer plays"
        )


class _AdtsInfo(AACInfo):
    def __init__(self, fileobj: BinaryIO, offset: Optional[int]):
        _require_adts(fileobj, offset or 0)
        fileobj.seek(0)
        super().__init__(fileobj)


class AdtsFile(ID3FileType):
    """A raw AAC file as mutagen reads one, with the ID3 tag it may start
    with as its tags.

    @raise UnplayableFile If what follows the tag is not AAC-LC or HE-AAC
        in ADTS frames.
    """

    _Info = _AdtsInfo
