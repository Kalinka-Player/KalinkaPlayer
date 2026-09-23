"""Pydantic models for Kalinka configuration.

Per-field UI metadata is carried in `Field(json_schema_extra=...)` with keys
recognized by the presentation emitter:

    widget       — one of the values of presentation_schema.Widget
    help         — inline sublabel / help text shown under the label
    importance   — "simple" | "expert" (default: "expert")
    setup        — "required" | "prompt" | "hidden" (default: "hidden")
    constraints  — dict with slider_min/slider_max/step/unit (merges with
                   Pydantic's own ge/le/etc.)
    inline       — on a nested model: no section of its own; its fields
                   render among the parent's, where it is declared. For a
                   group without its own ``enabled`` toggle, whose field
                   titles read on their own.

The default tier for unmarked fields is EXPERT — they're reachable only
through the about:config-style search. To put a field on the main
settings page (mandatory or frequently changed), tag it
``"importance": "simple"`` explicitly.

An ``importance`` on a KalinkaConfig member tags the whole *section*
instead, where the default is the other way round: a section shows up as
soon as one of its fields is simple, and ``"expert"`` hides the group
whatever its fields say.

``setup`` is a separate axis and says nothing about the tier: it is what
the app's first-run wizard asks for. Tag a field ``"required"`` when the
owning module cannot work without an answer (such a field must default
to empty), ``"prompt"`` when the default works but the choice is worth
making early. Everything else stays hidden from the wizard.

The General page is the sections below in declaration order, each named
by its ``title``, so field order here is the order the app renders.
"""

import os
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from kalinka_plugin_sdk import paths


# Shared extras — keeps audit-tagging consistent across the file.
_SIMPLE: dict[str, Any] = {"importance": "simple"}
_PROMPT: dict[str, Any] = {"setup": "prompt"}

# On a section, "expert" hides the group even if a field below turns simple.
_EXPERT_SECTION: dict[str, Any] = {"importance": "expert"}

# Release hosting the MiniLM text encoder (model.onnx + tokenizer.json are
# appended at fetch time). Same release the Jamendo mood index ships from —
# the index is built in this model's embedding space.
_MINILM_RELEASE = (
    "https://github.com/madenvel/KalinkaPlayer/releases/download/jamendo-ai-v1"
)


class LogLevel(str, Enum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


class ServerConfig(BaseModel):
    service_name: str = Field(
        default="My Kalinka Service",
        title="Service name",
        json_schema_extra={
            "help": "How this server appears in the app when found on your network",
            **_SIMPLE,
            **_PROMPT,
        },
    )
    interface: str = Field(
        default="all",
        title="Network interface",
        json_schema_extra={
            "help": 'Network connection the server listens on — "all" is right for most setups',
        },
    )
    port: int = Field(
        default=8000,
        title="Port",
        json_schema_extra={
            "help": "Port the app uses to connect to this server",
            "widget": "number_input",
            "constraints": {"ge": 1, "le": 65535},
            **_SIMPLE,
        },
    )
    log_level: LogLevel = Field(
        default=LogLevel.info,
        title="Log level",
    )
    auto_upgrade: bool = Field(
        default=False,
        title="Auto upgrade",
        json_schema_extra={
            "help": (
                "Install server updates automatically at night (between 3 "
                "and 6 AM) when nothing is playing; the server restarts "
                "itself when done"
            ),
            **_SIMPLE,
        },
    )
    # App-written when the first-run wizard finishes; expert-tier on purpose.
    oobe_complete: bool = Field(
        default=False,
        title="Initial setup complete",
        json_schema_extra={
            "help": (
                "Set when the app's first-run setup wizard finishes — "
                "reset to run the wizard again from a newly connected app"
            ),
        },
    )


class DeviceAutomationConfig(BaseModel):
    auto_power_on: bool = Field(
        default=True,
        title="Auto power on",
        json_schema_extra={
            "help": "Turn on device when playback starts",
            **_SIMPLE,
        },
    )
    auto_power_off: bool = Field(
        default=True,
        title="Auto power off",
        json_schema_extra={
            "help": "Turn off device when playback stops",
            **_SIMPLE,
        },
    )
    auto_off_timeout_seconds: int = Field(
        default=60,
        title="Pause timeout",
        json_schema_extra={
            "help": "Stop playback if paused for this many seconds (0 = disabled)",
            "constraints": {"unit": "s"},
            **_SIMPLE,
        },
    )


class SearchConfig(BaseModel):
    """Cross-source search tuning: the name matches ranked by
    ``kalinka_server.name_matches`` and the suggestion cards assembled by
    ``kalinka_server.ai_search``."""

    candidate_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        title="Name-match candidates per type",
        json_schema_extra={
            "help": (
                "How many results each source may return per entity type "
                "when matching a name — higher is more thorough but slower"
            ),
        },
    )
    ai_suggestions_limit: int = Field(
        default=20,
        ge=1,
        le=100,
        title="AI suggestions per source",
        json_schema_extra={
            "help": "How many tracks each source shows in its AI suggestions row",
        },
    )
    suggest_min_score: int = Field(
        default=25,
        ge=0,
        le=100,
        title="Search suggestion validation threshold",
        json_schema_extra={
            "help": (
                "How strongly a suggested search must match your library "
                "(0–100) to be offered — higher offers fewer, safer "
                "suggestions"
            ),
        },
    )


class EmbeddingConfig(BaseModel):
    """Shared MiniLM text embedder (kalinka_server.text_embedder) — one model
    instance serving the server and every plugin via the plugin context."""

    model_dir: str = Field(
        # Persistent state dir (<prefix>/var/lib/kalinka), same place the
        # module databases live — NOT the user home, which is read-only on
        # the device. paths.state_dir() honours $KALINKA_PREFIX.
        default_factory=lambda: os.path.join(
            paths.state_dir(), "models", "minilm"
        ),
        title="Text embedding model directory",
        json_schema_extra={
            "help": "Where the shared text-embedding model is stored",
        },
    )
    model_url: str = Field(
        default=_MINILM_RELEASE,
        title="Text embedding model download URL",
        json_schema_extra={
            "help": (
                "Where the model is downloaded from if missing — leave empty "
                "if you manage the files yourself"
            ),
        },
    )


class KalinkaConfig(BaseModel):
    """Main Kalinka configuration."""

    server: ServerConfig = Field(default_factory=ServerConfig, title="Server")
    device_automation: DeviceAutomationConfig = Field(
        default_factory=DeviceAutomationConfig, title="Device automation"
    )
    search: SearchConfig = Field(
        default_factory=SearchConfig,
        title="Search",
        json_schema_extra=_EXPERT_SECTION,
    )
    embedding: EmbeddingConfig = Field(
        default_factory=EmbeddingConfig,
        title="Text embedding",
        json_schema_extra=_EXPERT_SECTION,
    )
