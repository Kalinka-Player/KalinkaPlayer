#!/usr/bin/env python3
"""Render the root categories' banner JPEGs from their SVGs in ``artwork/``.

Needs ``rsvg-convert``. Published files are never rewritten: bump a category's
version in ``catalog_art.py`` to render changed art, so clients holding the old
link keep their file.

Usage:
    python scripts/render_catalog_artwork.py
"""

import io
import subprocess
from pathlib import Path

from PIL import Image

from kalinka_plugin_localfiles.catalog_art import (
    ART_CATEGORIES,
    ART_DIRECTORY,
    catalog_image,
)

SOURCES = Path(__file__).resolve().parents[1] / "artwork"
# The client's square crop of a 600x200 banner, which holds the icon.
SQUARE_LEFT = 330 / 600


def _target(url: str) -> Path:
    return ART_DIRECTORY / Path(url).name


def _rasterize(svg: Path, width: int, height: int) -> Image.Image:
    png = subprocess.run(
        ["rsvg-convert", "-w", str(width), "-h", str(height), str(svg)],
        check=True,
        capture_output=True,
    ).stdout
    return Image.open(io.BytesIO(png)).convert("RGB")


def _save(image: Image.Image, url: str) -> None:
    image.save(
        _target(url),
        format="JPEG",
        quality=90,
        optimize=True,
        subsampling=0,
    )


def main() -> None:
    for category in ART_CATEGORIES:
        svg = SOURCES / f"{category}.svg"
        links = catalog_image(category)
        if _target(links.large).exists():
            continue
        large = _rasterize(svg, 1200, 400)
        _save(large, links.large)
        _save(_rasterize(svg, 600, 200), links.small)
        left = round(large.width * SQUARE_LEFT)
        _save(large.crop((left, 0, left + large.height, large.height)), links.thumbnail)
        print(f"{category}: {Path(links.large).name}")


if __name__ == "__main__":
    main()
