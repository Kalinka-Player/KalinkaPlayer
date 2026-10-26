import os
from typing import Annotated, ClassVar
from enum import Enum

from pydantic import AfterValidator, ConfigDict, Field

from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.module_config import ModuleConfig

# Release hosting the int8 mood index (see kalinka-training
# jamendomaxcaps_embed.py). Fetched at startup if absent. The MiniLM query
# encoder is no longer a plugin asset — it is the server's shared text
# embedder (kalinka_server.text_embedder), configured under the server's
# "Text embedding" settings.
_RELEASE = (
    "https://github.com/Kalinka-Player/KalinkaPlayer/releases/download/jamendo-ai-v1"
)

# Settings saved before the repository moved still name its former owner, and
# that name would serve whoever registers it next.
_FORMER_REPO_URL = "https://github.com/madenvel/KalinkaPlayer/"
_REPO_URL = "https://github.com/Kalinka-Player/KalinkaPlayer/"


def _moved(url: str) -> str:
    if url.startswith(_FORMER_REPO_URL):
        return _REPO_URL + url.removeprefix(_FORMER_REPO_URL)
    return url


# Mood-index asset name. The index is fetched only when the local path is
# absent (see mood_search._provision), so a content change must rename the
# asset to force existing installs to re-download — overrides store only
# user-set values, so this default bump ships to everyone who hasn't pinned
# their own path. v2 = corporate/library-music pruned (~48.6k tracks removed;
# kalinka-training jamendomaxcaps_blocklist.py + jamendomaxcaps_prune.py).
_INDEX_ASSET = "jamendo_index_v2.sqlite"


class JamendoAudioFormat(str, Enum):
    """User-facing audio quality labels.

    The labels are what the settings UI shows; they map to Jamendo's
    ``audioformat`` codes (mp31/mp32/ogg/flac) in ``jamendo.FORMAT_CODE``.
    """

    MP3_VBR = "MP3 (VBR ~V0)"
    MP3_96 = "MP3 (96 kbps)"
    OGG = "OGG Vorbis"
    FLAC = "FLAC"


class JamendoConfig(ModuleConfig):
    """Jamendo input module settings."""

    model_config = ConfigDict(use_enum_values=True)

    __module_icon__: ClassVar[str] = "music_note_outlined"
    __module_icon_color__: ClassVar[str] = "#2E7D32"  # Jamendo green
    __preview_fields__: ClassVar[list[str]] = ["audio_format"]

    name: str = Field(
        default="jamendo",
        title="Jamendo",
        description=(
            "A catalogue of free, legally streamable music from independent "
            "artists. Needs a free Client ID from the Jamendo developer "
            "portal."
        ),
        frozen=True,
        exclude=True,
    )
    client_id: str = Field(
        default="",
        title="Client ID",
        description=(
            "Required for Jamendo to work. Create a free account and "
            "application at the "
            "[Jamendo Dev Portal](https://devportal.jamendo.com), then "
            "paste the application's Client ID here."
        ),
        json_schema_extra={
            "widget": "password",
            "importance": "simple",
            "setup": "required",
        },
    )
    audio_format: JamendoAudioFormat = Field(
        default=JamendoAudioFormat.MP3_VBR,
        title="Audio quality",
        description=(
            "Streaming format. FLAC is only available for tracks whose "
            "artist allowed lossless download and falls back to MP3 otherwise."
        ),
        json_schema_extra={"importance": "simple", "setup": "prompt"},
    )
    ai_search_enabled: bool = Field(
        default=True,
        title="Mood / AI search",
        description=(
            "Search Jamendo by mood or description, e.g. \"something "
            "melancholic for tonight\". The data it needs downloads "
            "automatically the first time."
        ),
        json_schema_extra={"importance": "simple", "setup": "prompt"},
    )
    ai_index_path: str = Field(
        # Persistent state dir (<prefix>/var/lib/kalinka), same place CLAP models
        # and localfiles.db live — NOT the user home, which is read-only on the
        # device. paths.state_dir() honours $KALINKA_PREFIX (dev-run vs prod).
        default_factory=lambda: os.path.join(
            paths.state_dir(), "jamendo", _INDEX_ASSET
        ),
        title="Mood index path",
        description="Where the mood-search index is stored.",
    )
    ai_index_url: Annotated[str, AfterValidator(_moved)] = Field(
        default=f"{_RELEASE}/{_INDEX_ASSET}",
        title="Mood index download URL",
        description=(
            "Downloaded automatically if the index is missing — leave empty "
            "if you manage the file yourself."
        ),
    )
