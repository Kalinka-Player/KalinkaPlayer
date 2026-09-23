from typing import Any, ClassVar
from pydantic import Field
from kalinka_plugin_sdk.module_config import ModuleConfig


# Fields default to the EXPERT tier (about:config search only). Mark a
# field "simple" to surface it on the main settings page.
_SIMPLE: dict[str, Any] = {"importance": "simple"}

# Independent of the tier: what the app's first-run wizard asks for. Both
# tagged fields describe how the receiver is wired, which no default can
# know, so the wizard asks even though playback starts without them.
_PROMPT: dict[str, Any] = {"setup": "prompt"}


class KalinkaPluginMusiccastConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "speaker_outlined"
    __preview_fields__: ClassVar[list[str]] = ["device_addr", "zone_name"]

    name: str = Field(
        default="musiccast",
        title="MusicCast",
        description=(
            "A Yamaha MusicCast receiver or speaker on your network. Kalinka "
            "switches its input and follows its volume and power state."
        ),
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False, title="Module enabled", json_schema_extra=_SIMPLE,
    )
    device_addr: str = Field(
        default="",
        title="Device IP address",
        json_schema_extra={
            "help": "IP address of your MusicCast device — leave empty to find it automatically",
            "private": True,
            **_SIMPLE,
        },
    )
    device_port: int = Field(
        default=80,
        title="Device port",
    )
    connected_input: str = Field(
        default="optical1",
        title="Connected input",
        json_schema_extra={
            "help": "The input on your receiver that the server's audio is wired into",
            **_SIMPLE,
            **_PROMPT,
        },
    )
    zone_name: str = Field(
        default="main",
        title="Zone name",
        json_schema_extra={**_SIMPLE, **_PROMPT},
    )
    auto_volume_correction: bool = Field(
        default=False,
        title="Auto volume correction",
        json_schema_extra={
            "help": "Even out volume differences between tracks (ReplayGain)",
            **_SIMPLE,
        },
    )
    volume_step_to_db: float = Field(
        default=0.5,
        title="Autoloudness volume step",
        ge=0.5,
        le=5,
        json_schema_extra={"constraints": {"unit": "dB"}},
    )
    discovery_timeout: int = Field(
        default=10,
        title="Discovery timeout",
        ge=5,
        le=60,
        json_schema_extra={"constraints": {"unit": "s"}},
    )
