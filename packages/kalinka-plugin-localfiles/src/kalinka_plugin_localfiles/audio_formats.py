"""Container identities shared by indexing and PCM-only analysis stages."""

from pathlib import PurePosixPath
from typing import Optional

from kalinka_plugin_sdk.media.formats import MEDIA_TYPES, is_dsd


#: What libsndfile, through which analysis reads PCM, cannot decode.
_UNDECODABLE_FOR_ANALYSIS = {"audio/mp4": "MP4", "audio/aac": "AAC"}


def analysis_refusal(path: str) -> Optional[str]:
    """Why a file's audio cannot be analysed as PCM, or None when it can."""
    if is_dsd(path):
        return "DSD audio analysis requires PCM conversion"
    media_type = MEDIA_TYPES.get(PurePosixPath(path).suffix.lower())
    container = _UNDECODABLE_FOR_ANALYSIS.get(media_type)
    if container is not None:
        return f"libsndfile cannot decode {container} audio for analysis"
    return None
