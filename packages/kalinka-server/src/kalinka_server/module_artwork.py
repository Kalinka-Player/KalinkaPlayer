"""Static plugin artwork, independent of plugin setup and playback health."""

import hashlib
import io
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException, Request, Response
from PIL import Image

from kalinka_plugin_sdk import ModuleConfig, ModuleIcon, ModuleIconFiles, ModuleIcons

_MAX_BYTES = 256 * 1024
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Asset:
    """@brief An installed icon's immutable bytes and public metadata."""

    data: bytes
    digest: str
    icon: ModuleIcon


def _read(path: Path, min_height: float) -> _Asset:
    with Path(path).open("rb") as stream:
        data = stream.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("Icon exceeds 256 KiB")
    if path.suffix.lower() == ".svg":
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("SVG declarations are not supported")
        root = ElementTree.fromstring(data)
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            raise ValueError("Not an SVG image")
        _, _, width, height = map(
            float, root.attrib["viewBox"].replace(",", " ").split()
        )
        mime = "image/svg+xml"
    elif path.suffix.lower() == ".png":
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("Not a PNG image")
            width, height = image.size
            image.verify()
        mime = "image/png"
    else:
        raise ValueError("Icons must be SVG or PNG")
    if width <= 0 or height <= 0:
        raise ValueError("Icon dimensions must be positive")
    digest = hashlib.sha256(data).hexdigest()
    return _Asset(
        data,
        digest,
        ModuleIcon(
            url=f"/server/module-icons/{digest}",
            mime_type=mime,
            aspect_ratio=width / height,
            min_height=min_height,
        ),
    )


@lru_cache(maxsize=128)
def _assets(files: ModuleIconFiles) -> tuple[_Asset, _Asset] | None:
    try:
        return (
            _read(Path(files.small), files.min_height),
            _read(Path(files.standard), files.min_height),
        )
    except (
        OSError,
        SyntaxError,
        ValueError,
        KeyError,
        ElementTree.ParseError,
        Image.DecompressionBombError,
    ):
        _LOG.warning(
            "Plugin artwork is unavailable or invalid; using its fallback icon"
        )
        return None


def module_icons(config: ModuleConfig) -> ModuleIcons | None:
    """@brief Resolve packaged artwork, falling back to declared image URLs."""
    files = config.__module_icon_files__
    if files is not None and (assets := _assets(files)) is not None:
        small, standard = assets
        return ModuleIcons(
            small=small.icon, standard=standard.icon, monochrome=files.monochrome
        )
    return config.__module_icons__


def register_module_icon_routes(
    app: FastAPI, configs: Callable[[], Iterable[ModuleConfig]]
) -> None:
    """@brief Serve only artwork declared by installed modules and devices."""

    @app.api_route("/server/module-icons/{digest}", methods=["GET", "HEAD"])
    async def icon(digest: str, request: Request):
        for config in configs():
            files = config.__module_icon_files__
            if files is None or (assets := _assets(files)) is None:
                continue
            for asset in assets:
                if asset.digest != digest:
                    continue
                headers = {
                    "ETag": f'"{digest}"',
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                }
                tags = request.headers.get("if-none-match", "").split(",")
                if any(
                    tag.strip().removeprefix("W/") in {"*", headers["ETag"]}
                    for tag in tags
                ):
                    return Response(status_code=304, headers=headers)
                headers["Content-Length"] = str(len(asset.data))
                return Response(
                    b"" if request.method == "HEAD" else asset.data,
                    media_type=asset.icon.mime_type,
                    headers=headers,
                )
        raise HTTPException(status_code=404, detail="Icon not found")
