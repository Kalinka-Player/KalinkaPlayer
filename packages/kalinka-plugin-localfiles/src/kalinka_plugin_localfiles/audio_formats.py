"""Container identities shared by indexing and PCM-only analysis stages."""

from pathlib import PurePosixPath

DSD_MEDIA_TYPES = {".dsf": "audio/x-dsf", ".dff": "audio/x-dff"}

# Every format the indexer reads, by one name on every host: Python's own type
# table lacks some, not every host has /etc/mime.types, and an older one calls
# FLAC audio/x-flac.
MEDIA_TYPES = {
    **DSD_MEDIA_TYPES,
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
}


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
