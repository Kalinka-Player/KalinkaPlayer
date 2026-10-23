"""Composed art for browse items. Pure, synchronous Pillow/NumPy.

Two shapes, one per :class:`ArtStyle`. :func:`render_playlist_cover` gives a
list of tracks the square cover such a list has always had — a 2x2 mosaic of
its albums, or a single album when there are not four to mosaic.

:func:`render_catalog_art` renders the whole tile the app displays full-bleed:
a generated background (a non-linear diagonal gradient with seeded concentric
geometry drawn over it, plus film grain) with an album cascade on the right
when covers are available. :func:`render_catalog_thumbnail` renders the same
art square for a row's thumbnail: the background without the text column, and
the cascade gathered to the middle.
No text, chevron or frame — the app draws the icon/title/description column on
the left and strokes a source-coloured frame around the tile. The gradient key
colour is derived from the artwork (dominant cover colour, or a seeded hue for
cover-less catalogs), not the source. Rendering is deterministic in its inputs;
STYLE_VERSION is part of the content fingerprint, so bumping it regenerates
every tile.
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import math
from enum import Enum
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

STYLE_VERSION = 4

CANVAS_W = 1200
CANVAS_H = 400  # 3:1

COVER_SIDE = 640
THUMBNAIL_SIDE = 480


class ArtStyle(Enum):
    """What an item's art is: the wide background behind a catalog card, or
    the square cover of a list of tracks."""

    CARD = "card"
    COVER = "cover"

    @property
    def aspect_ratio(self) -> float:
        return CANVAS_W / CANVAS_H if self is ArtStyle.CARD else 1.0


# Background palette.
_BASE = (16, 16, 20)          # near-black ground under the gradient
_GRAD_START = 0.42            # diagonal t where the gradient starts to build
_GRAIN = 5.0                 # film-grain sigma
# Horizontal darkening so the left (the app's text column) is near-black.
_LEFT_DARK = 0.92            # strength toward black on the far left
_LEFT_DARK_HOLD = 0.38       # fully dark up to this x fraction
_LEFT_DARK_END = 0.60        # eased back to the artwork by this x fraction

# Album cascade, front tile last: (centre x / W, centre y / H, angle). Shifted
# left of the right edge so the app can place a chevron there.
_COLLAGE_LAYOUT = [
    (0.525, 0.46, -7.0),
    (0.645, 0.50, 2.0),
    (0.765, 0.53, 9.0),
]
_COVER_FRACTION = 0.68  # tile side as a fraction of canvas height
# The thumbnail's cascade by cover count, gathered to the middle of a square.
_THUMBNAIL_LAYOUTS = {
    1: [(0.5, 0.5, 2.0)],
    2: [(0.42, 0.47, -6.0), (0.58, 0.53, 6.0)],
    3: [(0.36, 0.45, -7.0), (0.5, 0.5, 2.0), (0.64, 0.55, 9.0)],
}
_THUMBNAIL_COVER_FRACTION = 0.56
_SUPERSAMPLE = 2        # render at 2x for clean rings and rotated edges

# Rich, dark-saturated key colour so the wash reads regardless of source hue.
_KEY_SAT = 0.60
_KEY_LIGHT = 0.32


def _rng(seed: str) -> np.random.Generator:
    digest = hashlib.sha256(f"{STYLE_VERSION}:{seed}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def _hue_color(hue_deg: float, saturation: float, lightness: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((hue_deg % 360.0) / 360.0, lightness, saturation)
    return (int(r * 255), int(g * 255), int(b * 255))


def _dominant_color(image: Image.Image) -> tuple[int, int, int]:
    """Most vivid colour of the image, saturation-weighted so glows don't
    average to mud."""
    thumb = image.convert("RGB").resize((12, 12), Image.Resampling.LANCZOS)
    pixels = np.asarray(thumb, dtype=np.float32) / 255.0
    flat = pixels.reshape(-1, 3)
    maxc = flat.max(axis=1)
    minc = flat.min(axis=1)
    sat = np.where(maxc > 0, (maxc - minc) / np.maximum(maxc, 1e-6), 0.0)
    score = sat * np.clip(maxc, 0.15, 0.9)
    r, g, b = flat[int(score.argmax())]
    return (int(r * 255), int(g * 255), int(b * 255))


def _key_color(covers: Sequence[Image.Image], seed: str) -> tuple[int, int, int]:
    """Gradient key: the dominant cover hue for cover catalogs, else a hue
    seeded from the id (so cover-less catalogs vary), at a fixed rich tone."""
    if covers:
        h, _, _ = colorsys.rgb_to_hls(*(c / 255.0 for c in _dominant_color(covers[0])))
        return _hue_color(h * 360.0, _KEY_SAT, _KEY_LIGHT)
    hue = int.from_bytes(hashlib.md5(seed.encode()).digest()[:2], "big") % 360
    return _hue_color(hue, _KEY_SAT, _KEY_LIGHT + 0.02)


def _draw_geometry(base: Image.Image, rng: np.random.Generator) -> None:
    """Seeded concentric bands + satellites drawn over the gradient, so every
    card has visible, deterministic geometry rather than a flat wash. Biased to
    the mid/right, where the left darkening doesn't swallow it. Inner discs
    overwrite outer ones, leaving soft alternating concentric bands."""
    width, height = base.size
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    cx = width * (0.48 + rng.random() * 0.42)
    cy = height * (0.1 + rng.random() * 0.8)
    maxr = height * (1.1 + rng.random() * 0.9)
    rings = 6 + int(rng.random() * 5)
    for i in range(rings, 0, -1):
        r = maxr * i / rings
        fill = (255, 255, 255, 22) if i % 2 else (0, 0, 0, 30)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)

    for _ in range(2 + int(rng.random() * 3)):
        ang = rng.random() * 2 * math.pi
        dist = (0.5 + rng.random() * 0.6) * maxr
        sr = height * (0.04 + rng.random() * 0.07)
        sx = cx + math.cos(ang) * dist
        sy = cy + math.sin(ang) * dist
        draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 255, 255, 26))

    base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(round(height * 0.02))))


def _background(
    width: int,
    height: int,
    key: tuple[int, int, int],
    rng: np.random.Generator,
    *,
    text_column: bool = True,
) -> Image.Image:
    """Near-black ground under a non-linear diagonal gradient (black -> key),
    with seeded concentric geometry on top, and the left held near-black for the
    app's text column where there is one."""
    xs, ys = np.linspace(0, 1, width), np.linspace(0, 1, height)
    grid_x, grid_y = np.meshgrid(xs, ys)
    t = (grid_x + grid_y) * 0.5  # 0 at top-left, 1 at bottom-right

    ground = np.empty((height, width, 3), float)
    ground[:] = _BASE
    col = t[..., None] * np.array(key, float)
    u = np.clip((t - _GRAD_START) / (1 - _GRAD_START), 0, 1)
    a = (u * u * (3 - 2 * u))[..., None]  # smoothstep ease-in
    arr = ground * (1 - a) + col * a
    result = Image.fromarray(arr.astype("uint8"), "RGB").convert("RGBA")

    # Geometry over the gradient (so it isn't erased by it), then hold the left
    # near-black for the text column — the scrim darkens any bands that reach it.
    _draw_geometry(result, rng)
    if not text_column:
        return result

    ld = np.clip(
        (_LEFT_DARK_END - grid_x) / (_LEFT_DARK_END - _LEFT_DARK_HOLD), 0, 1
    )
    ld = ld * ld * (3 - 2 * ld)  # smoothstep, holding full-dark on the far left
    scrim = np.zeros((height, width, 4), dtype="uint8")
    scrim[..., 3] = (ld * _LEFT_DARK * 255).astype("uint8")
    result.alpha_composite(Image.fromarray(scrim, "RGBA"))
    return result


def _fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale keeping aspect and centre-crop to exactly width x height."""
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _rounded_tile(cover: Image.Image, side: int, radius: int) -> Image.Image:
    """RGBA square cover tile: rounded corners + faint light border."""
    scale = 4  # supersample the corner mask
    tile = _fit_cover(cover.convert("RGB"), side, side)
    mask = Image.new("L", (side * scale, side * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, side * scale - 1, side * scale - 1], radius=radius * scale, fill=255
    )
    mask = mask.resize((side, side), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    border = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [0, 0, side - 1, side - 1], radius=radius, outline=(255, 255, 255, 36), width=1
    )
    return Image.alpha_composite(out, border)


def _paste_with_shadow(
    base: Image.Image, tile: Image.Image, center: tuple[int, int], angle_deg: float
) -> None:
    """Rotate a tile, drop a soft shadow under it, paste both — shadow sized to
    the tile so it scales with the canvas."""
    rotated = tile.rotate(angle_deg, expand=True, resample=Image.Resampling.BICUBIC)
    blur = max(4, round(rotated.width * 0.03))
    shadow_src = Image.new("RGBA", rotated.size, (0, 0, 0, 0))
    shadow_src.paste(
        (0, 0, 0, 255), (0, 0), rotated.getchannel("A").point(lambda a: int(a * 0.5))
    )
    pad = blur * 3
    shadow = Image.new(
        "RGBA", (rotated.width + pad * 2, rotated.height + pad * 2), (0, 0, 0, 0)
    )
    shadow.paste(shadow_src, (pad, pad))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    drop = max(3, round(rotated.width * 0.025))
    base.alpha_composite(
        shadow, (center[0] - shadow.width // 2, center[1] - shadow.height // 2 + drop)
    )
    base.alpha_composite(
        rotated, (center[0] - rotated.width // 2, center[1] - rotated.height // 2)
    )


def _collage(
    base: Image.Image,
    covers: Sequence[Image.Image],
    width: int,
    height: int,
    layout: Sequence[tuple[float, float, float]] = _COLLAGE_LAYOUT,
    cover_fraction: float = _COVER_FRACTION,
) -> None:
    side = round(height * cover_fraction)
    radius = round(side * 0.055)
    picks = list(covers[: len(layout)])
    for idx in reversed(range(len(picks))):  # back to front
        cx_f, cy_f, angle = layout[idx]
        tile = _rounded_tile(picks[idx], side, radius)
        _paste_with_shadow(base, tile, (round(width * cx_f), round(height * cy_f)), angle)


def _grain(image: Image.Image, rng: np.random.Generator, strength: float) -> Image.Image:
    arr = np.asarray(image, dtype=np.float32)
    noise = rng.standard_normal((image.height, image.width, 1)).astype(np.float32) * strength
    return Image.fromarray(np.clip(arr + noise, 0, 255).astype("uint8"), "RGB")


def render_catalog_art(
    covers: Sequence[Image.Image],
    names: Sequence[str],
    seed: str,
    *,
    width: int = CANVAS_W,
    height: int = CANVAS_H,
) -> Image.Image:
    """Render one opaque tile: generated background + album cascade (when covers
    are present). ``names`` is unused (the app draws text) but kept for a stable
    call/fingerprint signature."""
    rng = _rng(seed)
    key = _key_color(covers, seed)
    ss = _SUPERSAMPLE
    base = _background(width * ss, height * ss, key, rng)
    if covers:
        _collage(base, covers, width * ss, height * ss)
    out = base.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    return _grain(out, rng, _GRAIN)


def render_catalog_thumbnail(
    covers: Sequence[Image.Image], seed: str, *, side: int = THUMBNAIL_SIDE
) -> Image.Image:
    """The card's art, square: its background without the text column, and up
    to three covers gathered to the middle."""
    rng = _rng(seed)
    key = _key_color(covers, seed)
    ss = _SUPERSAMPLE
    base = _background(side * ss, side * ss, key, rng, text_column=False)
    picks = list(covers[:3])
    if picks:
        _collage(
            base,
            picks,
            side * ss,
            side * ss,
            _THUMBNAIL_LAYOUTS[len(picks)],
            _THUMBNAIL_COVER_FRACTION,
        )
    out = base.convert("RGB").resize((side, side), Image.Resampling.LANCZOS)
    return _grain(out, rng, _GRAIN)


def render_playlist_cover(
    covers: Sequence[Image.Image], *, side: int = COVER_SIDE
) -> Image.Image:
    """Square cover for a list of tracks: a 2x2 mosaic of four album covers,
    or the first album alone below four — a half-filled grid reads as a
    mistake rather than as a cover. Needs at least one cover."""
    cell = side // 2
    if len(covers) >= 4:
        mosaic = Image.new("RGB", (cell * 2, cell * 2))
        for index, cover in enumerate(covers[:4]):
            mosaic.paste(
                _fit_cover(cover.convert("RGB"), cell, cell),
                ((index % 2) * cell, (index // 2) * cell),
            )
        return mosaic
    return _fit_cover(covers[0].convert("RGB"), side, side)


def encode_jpeg(image: Image.Image, quality: int = 85) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer, format="JPEG", quality=quality, progressive=True, optimize=True
    )
    return buffer.getvalue()


def content_fingerprint(
    cover_bytes: Sequence[bytes],
    names: Sequence[str],
    seed: str,
    style: ArtStyle = ArtStyle.CARD,
) -> str:
    """Content identity (style version + inputs); unchanged -> cache is current."""
    hasher = hashlib.sha1()
    hasher.update(f"style:{STYLE_VERSION}".encode())
    hasher.update(f"shape:{style.value}".encode())
    hasher.update(f"seed:{seed}".encode())
    for blob in cover_bytes:
        hasher.update(b"cover:")
        hasher.update(hashlib.sha1(blob).digest())
    for name in names:
        hasher.update(f"name:{name}".encode())
    return hasher.hexdigest()
