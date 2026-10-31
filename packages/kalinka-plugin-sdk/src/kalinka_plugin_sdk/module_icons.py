from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class ModuleIcon(BaseModel):
    """@brief SVG or PNG artwork accessible without credentials.

    @param url An absolute HTTP(S) URL or a path rooted at the Kalinka server.
    @param aspect_ratio Width divided by height.
    @param min_height Minimum displayed height in logical pixels.
    @note SVGs must be self-contained, without scripts, external resources or
        embedded raster images.
    """

    url: str
    mime_type: Literal["image/svg+xml", "image/png"]
    aspect_ratio: float = Field(default=1.0, gt=0, le=8, allow_inf_nan=False)
    min_height: float = Field(default=0, ge=0, le=64, allow_inf_nan=False)

    @field_validator("url")
    @classmethod
    def _image_url(cls, value: str) -> str:
        if not value or any(char.isspace() for char in value) or "\\" in value:
            raise ValueError("An icon needs an HTTP(S) URL or a server-rooted path")
        parts = urlsplit(value)
        if value.startswith("/") and not value.startswith("//"):
            return value
        if (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and parts.password is None
        ):
            return value
        raise ValueError("An icon needs an HTTP(S) URL or a server-rooted path")


class ModuleIcons(BaseModel):
    """@brief Attribution and source icons, with a shared tinting policy.

    @param small Artwork for compact attribution.
    @param standard Artwork for other source displays.
    @param monochrome Allow black/white tinting; otherwise preserve original colors.
    @note Both variants are required but may reference the same SVG.
    """

    small: ModuleIcon
    standard: ModuleIcon
    monochrome: bool = False


@dataclass(frozen=True)
class ModuleIconFiles:
    """@brief Packaged icons served even when the plugin is disabled.

    @param small Local SVG/PNG for compact attribution.
    @param standard Local SVG/PNG for other source displays.
    @param monochrome Allow black/white tinting; otherwise preserve original colors.
    @param min_height Minimum displayed height in logical pixels for both variants.
    @note Include files as package data, at most 256 KiB each. Changes require
        a server restart.
    """

    small: Path
    standard: Path
    monochrome: bool = False
    min_height: float = 0
