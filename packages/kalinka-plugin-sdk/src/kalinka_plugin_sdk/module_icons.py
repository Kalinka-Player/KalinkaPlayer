"""Artwork a plugin supplies for its attribution and other source displays."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class ModuleIcon(BaseModel):
    """@brief SDK 3.18: a static SVG or PNG served at a public image URL.

    @param url An absolute HTTP(S) URL or a path rooted at the Kalinka server.
    @param mime_type The image format; clients must not infer it from the URL.
    @param aspect_ratio Width divided by height, reserving layout before loading.
    @note SVGs must be self-contained, without scripts, external resources or
        embedded raster images. Clients preserve the artwork's aspect ratio.
    """

    url: str
    mime_type: Literal["image/svg+xml", "image/png"]
    aspect_ratio: float = Field(default=1.0, gt=0, le=8, allow_inf_nan=False)

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
    """@brief SDK 3.18: both sizes of a plugin's optional source artwork.

    @param small Artwork for compact attribution.
    @param standard Artwork for other source displays, scaled to fit.
    @param monochrome Whether clients may render the alpha mask in black on
        light backgrounds and white on dark backgrounds, never a source tint.
    @note Both variants are required. They may reference the same SVG. False
        monochrome preserves the supplied colors. Clients that cannot load or
        draw an image fall back to the module's Material icon or initial.
    """

    small: ModuleIcon
    standard: ModuleIcon
    monochrome: bool = False


@dataclass(frozen=True)
class ModuleIconFiles:
    """@brief SDK 3.18: packaged artwork the server serves even while disabled.

    @param small Local SVG/PNG for compact attribution.
    @param standard Local SVG/PNG for other source displays.
    @param monochrome Whether the artwork permits black/white rendering.
    @note Files are immutable for the lifetime of the server process. Ship
        them as package data; each must be at most 256 KiB.
    """

    small: Path
    standard: Path
    monochrome: bool = False
