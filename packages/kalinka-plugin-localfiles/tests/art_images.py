"""Small test pictures that differ the way covers do.

Solid colours all share one dHash (every neighbour is equal), so tests that
tell covers apart by their hash need a pattern: a seeded 8x8 grid scaled up
without smoothing gives each seed a hash of its own.
"""

import io

import numpy as np
from PIL import Image


def patterned_image(seed: int, size: int = 64) -> Image.Image:
    grid = np.random.default_rng(seed).integers(0, 256, (8, 8, 3), dtype=np.uint8)
    return Image.fromarray(grid, "RGB").resize((size, size), Image.Resampling.NEAREST)


def patterned_png(seed: int, size: int = 64) -> bytes:
    buf = io.BytesIO()
    patterned_image(seed, size).save(buf, "PNG")
    return buf.getvalue()
