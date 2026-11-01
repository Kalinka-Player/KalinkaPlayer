"""Which files are music Kalinka plays, and what each is called."""

import mimetypes
import os
from pathlib import PurePosixPath
from typing import Optional

from ..files import is_hidden_file

DSD_MEDIA_TYPES = {".dsf": "audio/x-dsf", ".dff": "audio/x-dff"}

# Every format the readers here understand, by one name on every host:
# Python's own type table lacks some, not every host has /etc/mime.types, and
# an older one calls FLAC audio/x-flac.
MEDIA_TYPES = {
    **DSD_MEDIA_TYPES,
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
    # Some rippers name M4A files holding Apple Lossless this way.
    ".alac": "audio/mp4",
    ".aac": "audio/aac",
}

SUPPORTED_AUDIO_EXTENSIONS = frozenset(MEDIA_TYPES)


def is_dsd(path: str, media_type: str = "") -> bool:
    return PurePosixPath(
        path
    ).suffix.lower() in DSD_MEDIA_TYPES or media_type.lower().split(";", 1)[
        0
    ].strip() in (
        "audio/x-dsf",
        "audio/dsf",
        "audio/x-dff",
        "audio/dff",
        "audio/dsd",
    )


def media_type_of(path: str) -> Optional[str]:
    """The media type a location's name implies, or None.

    Guessed from the last component alone, because
    :func:`mimetypes.guess_type` treats a scheme-bearing string as a URL and
    truncates it at the first ``#`` or ``?`` — which silently unnames every
    share-backed track called something like ``Symphony #5.flac``. The type
    decides which tag reader runs and what a renderer is told it is playing,
    so getting it from the name is not optional.
    """
    suffix = os.path.splitext(path)[1].lower()
    return MEDIA_TYPES.get(suffix) or mimetypes.guess_type(os.path.basename(path))[0]


def is_supported_audio_file(filename: str) -> bool:
    """Whether a file is music Kalinka plays. A hidden one never is,
    whatever it is called: the ``._`` forks macOS leaves beside a copied
    track carry its name and extension and no audio."""
    if is_hidden_file(os.path.basename(filename)):
        return False
    return os.path.splitext(filename.lower())[1] in SUPPORTED_AUDIO_EXTENSIONS
