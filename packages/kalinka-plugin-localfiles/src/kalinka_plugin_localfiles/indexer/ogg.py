"""Reading an Ogg file as the Ogg Vorbis the renderer plays."""

from typing import BinaryIO

from mutagen.ogg import OggPage
from mutagen.oggvorbis import OggVorbis


class NoVorbisStream(Exception):
    """An Ogg file none of whose streams is Vorbis: Opus or FLAC, say."""


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
            if page.packets and page.packets[0].startswith(b"\x01vorbis"):
                return True
            page = OggPage(audio)
    except EOFError:
        pass
    return False


def open_ogg_vorbis(audio: BinaryIO) -> OggVorbis:
    """Parse an open Ogg Vorbis file with mutagen.

    @raise NoVorbisStream If none of its streams is Vorbis.
    """
    if not starts_vorbis_stream(audio):
        raise NoVorbisStream()
    audio.seek(0)
    return OggVorbis(audio)
