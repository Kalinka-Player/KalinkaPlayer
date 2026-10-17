"""Container identities shared by indexing and PCM-only analysis stages."""

from pathlib import PurePosixPath
from typing import Optional

DSD_MEDIA_TYPES = {".dsf": "audio/x-dsf", ".dff": "audio/x-dff"}

# Every format the indexer reads, by one name on every host: Python's own type
# table lacks some, not every host has /etc/mime.types, and an older one calls
# FLAC audio/x-flac.
MEDIA_TYPES = {
    **DSD_MEDIA_TYPES,
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
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


#: What libsndfile, through which analysis reads PCM, cannot decode.
_UNDECODABLE_FOR_ANALYSIS = {".m4a": "MP4", ".aac": "AAC"}


def analysis_refusal(path: str) -> Optional[str]:
    """Why a file's audio cannot be analysed as PCM, or None when it can."""
    if is_dsd(path):
        return "DSD audio analysis requires PCM conversion"
    container = _UNDECODABLE_FOR_ANALYSIS.get(PurePosixPath(path).suffix.lower())
    if container is not None:
        return f"libsndfile cannot decode {container} audio for analysis"
    return None
