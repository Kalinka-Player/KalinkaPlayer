"""A cover for music that has none of its own, made from the covers of what
it holds: four as a grid, fewer as the first alone — a half-filled grid reads
as a mistake rather than as a cover."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence

from PIL import Image, ImageOps

logger = logging.getLogger(__name__.split(".")[-1])

MOSAIC_TILES = 4
MOSAIC_SIDE = 600


def render_mosaic(
    covers: Sequence[Image.Image], side: int = MOSAIC_SIDE
) -> Image.Image:
    """Exactly :data:`MOSAIC_TILES` covers as a 2x2 grid in reading order,
    each cropped square about its centre."""
    if len(covers) != MOSAIC_TILES:
        raise ValueError(f"a mosaic takes {MOSAIC_TILES} covers, not {len(covers)}")
    cell = side // 2
    mosaic = Image.new("RGB", (cell * 2, cell * 2))
    for index, cover in enumerate(covers):
        tile = ImageOps.fit(
            cover.convert("RGB"), (cell, cell), Image.Resampling.LANCZOS
        )
        mosaic.paste(tile, ((index % 2) * cell, (index // 2) * cell))
    return mosaic


def compose_cover(sources: Sequence[Path]) -> Optional[Image.Image]:
    """The cover for music whose first covers are ``sources``, in order: the
    mosaic of the first four that open, or the first alone when fewer do.
    None when none opens."""
    covers = _open_covers(sources, MOSAIC_TILES)
    if not covers:
        return None
    if len(covers) == MOSAIC_TILES:
        return render_mosaic(covers)
    return covers[0].convert("RGB")


def _open_covers(sources: Sequence[Path], wanted: int) -> List[Image.Image]:
    covers: List[Image.Image] = []
    for source in sources:
        if len(covers) == wanted:
            break
        try:
            with Image.open(source) as image:
                image.load()
                covers.append(image.copy())
        except (OSError, ValueError) as e:
            logger.warning(f"Skipping cover {source}: {e}")
    return covers
