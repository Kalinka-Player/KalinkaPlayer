"""Container identities shared by indexing and PCM-only analysis stages."""

from pathlib import PurePosixPath

DSD_MEDIA_TYPES = {".dsf": "audio/x-dsf", ".dff": "audio/x-dff"}


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
