"""Catalog cards tint one of three Kalinka banner geometries before adding up to
three album covers.

The app overlays labels on the dark left surface; square thumbnails gather the
covers in the middle. Cards without artwork show one transparent cabinet matching
their contents. Playlist covers retain their album-only mosaic.
STYLE_VERSION participates in cache fingerprints and must change with the art.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import unicodedata
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageOps

STYLE_VERSION = 12

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


_ART_DIRECTORY = Path(__file__).with_name("assets") / "catalog"
BACKGROUND_DESIGNS = ("arcs", "folds", "rings")
BACKGROUND_TINTS = (
    "#814354",
    "#7E4D3E",
    "#7D653B",
    "#696C3D",
    "#4F6543",
    "#3A644C",
    "#386762",
    "#44617E",
    "#566174",
    "#4F5482",
    "#60507C",
    "#6C4A73",
    "#774668",
    "#6E5F53",
)
BACKGROUND_BASE = "#1B1B1E"
#: The tint's share of the base in the left surface, the right surface, the
#: geometry and its fine line.
BACKGROUND_WEIGHTS = (0.12, 0.34, 0.48, 0.68)
_PLACEHOLDER_KINDS = ("album", "catalog", "track", "playlist", "artist")
_GRAIN = 0.5

# Keep the left 42% clear for labels and the far right clear for the chevron.
_CARD_LAYOUTS = {
    1: [(0.72, 0.50, -3.0)],
    2: [(0.645, 0.48, -6.0), (0.79, 0.52, 6.0)],
    3: [(0.56, 0.46, -7.0), (0.69, 0.50, 1.0), (0.82, 0.54, 8.0)],
}
_COVER_FRACTION = 0.64
_THUMBNAIL_LAYOUTS = {
    1: [(0.5, 0.5, 2.0)],
    2: [(0.42, 0.47, -6.0), (0.58, 0.53, 6.0)],
    3: [(0.36, 0.45, -7.0), (0.5, 0.5, 2.0), (0.64, 0.55, 9.0)],
}
_THUMBNAIL_COVER_FRACTION = 0.56
_SUPERSAMPLE = 2


def _rng(seed: str) -> np.random.Generator:
    digest = hashlib.sha256(f"{STYLE_VERSION}:{seed}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def background_text_key(names: Sequence[str], seed: str) -> str:
    """Stable identity from the first nonempty title, or the id when absent."""
    parts = [
        " ".join(unicodedata.normalize("NFKC", name).casefold().split())
        for name in names
    ]
    parts = [part for part in parts if part]
    text = json.dumps(parts[:1], ensure_ascii=False) if parts else f"id:{seed}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def background_design(text_key: str) -> int:
    return int(text_key[:16], 16) % len(BACKGROUND_DESIGNS)


def background_tint(text_key: str) -> str:
    return BACKGROUND_TINTS[int(text_key[16:32], 16) % len(BACKGROUND_TINTS)]


def _blend(tint: str, weight: float) -> tuple[int, int, int]:
    base, rgb = ImageColor.getrgb(BACKGROUND_BASE), ImageColor.getrgb(tint)
    return tuple(round(a * (1 - weight) + b * weight) for a, b in zip(base, rgb))


def background_swatches(tint: str) -> tuple[str, ...]:
    """The design's four flat colours: each weight of *tint* over the base."""
    return tuple(
        "#{:02X}{:02X}{:02X}".format(*_blend(tint, weight))
        for weight in BACKGROUND_WEIGHTS
    )


_DESIGN_W = 600
_LEFT, _RIGHT, _SHAPE, _LINE = BACKGROUND_WEIGHTS


def _cubic(*controls: tuple[float, float], steps: int = 64) -> np.ndarray:
    p0, p1, p2, p3 = (np.asarray(point, dtype=np.float64) for point in controls)
    t = np.linspace(0, 1, steps)[:, None]
    return (
        (1 - t) ** 3 * p0
        + 3 * (1 - t) ** 2 * t * p1
        + 3 * (1 - t) * t**2 * p2
        + t**3 * p3
    )


def _upper_arc(cx: float, cy: float, radius: float, steps: int = 256) -> np.ndarray:
    t = np.linspace(math.pi, 2 * math.pi, steps)
    return np.stack([cx + radius * np.cos(t), cy + radius * np.sin(t)], axis=1)


def _stroke(
    path: np.ndarray | Sequence[tuple[float, float]], width: float
) -> np.ndarray:
    """The outline of a stroke along *path*. Every stroke in the designs ends
    off the canvas, so their round caps never show and are left out."""
    points = np.asarray(path, dtype=np.float64)
    tangent = np.gradient(points, axis=0)
    normal = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
    normal *= width / 2 / np.linalg.norm(normal, axis=1, keepdims=True)
    return np.concatenate([points + normal, (points - normal)[::-1]])


# Each design's shapes in its 600x200 viewBox, back to front.
_GEOMETRY: dict[str, tuple[tuple[float, np.ndarray], ...]] = {
    "arcs": (
        (
            _SHAPE,
            np.concatenate(
                [
                    _cubic((291, 220), (360, 73), (458, -12), (602, -37)),
                    _cubic((638, -23), (510, -8), (391, 83), (326, 220)),
                ]
            ),
        ),
        (_LINE, _stroke(_cubic((347, 220), (406, 103), (496, 31), (608, -9)), 1.05)),
        (_SHAPE, _stroke(_cubic((365, 220), (426, 111), (506, 49), (612, 7)), 1.25)),
    ),
    "folds": (
        (_SHAPE, np.array([(289, 213), (452, -12), (500, -12), (337, 213)])),
        (_LINE, _stroke([(359, 213), (522, -12)], 1.05)),
        (_SHAPE, _stroke([(379, 214), (542, -12)], 1.2)),
        (_SHAPE, np.array([(480, 213), (640, -9), (640, 50), (523, 213)])),
    ),
    "rings": (
        *(
            (_SHAPE, _stroke(_upper_arc(480, 201, radius), width))
            for radius, width in ((114, 1.2), (128, 1.2), (143, 1.2), (167, 21))
        ),
        (_LINE, _stroke(_upper_arc(480, 201, 190), 1.0)),
    ),
}


@lru_cache(maxsize=len(BACKGROUND_DESIGNS))
def _backdrop(variant: int) -> Image.Image:
    """A design as the tint's share at each pixel, 0-255, at the card's
    supersampled size. Every colour in it is the tint over the base, so one
    weight map serves every tint."""
    width, height = CANVAS_W * _SUPERSAMPLE, CANVAS_H * _SUPERSAMPLE
    # Drawn at twice the size and reduced, to antialias the hairlines.
    scale = 2 * width / _DESIGN_W
    across = (np.arange(2 * width) + 0.5) / (2 * width)
    wash = _LEFT + (_RIGHT - _LEFT) * np.clip((across - 0.40) / 0.60, 0, 1)
    source = Image.fromarray(
        np.repeat(np.round(wash * 255).astype("uint8")[None, :], 2 * height, axis=0)
    )
    draw = ImageDraw.Draw(source)
    for weight, outline in _GEOMETRY[BACKGROUND_DESIGNS[variant]]:
        draw.polygon(
            [(x * scale, y * scale) for x, y in outline], fill=round(weight * 255)
        )
    return source.reduce(2)


def _tint_background(source: Image.Image, text_key: str) -> Image.Image:
    """Opaque RGBA: each share in *source* as that much of the tint over the base."""
    ramp = [_blend(background_tint(text_key), share / 255) for share in range(256)]
    channels = [source.point([color[c] for color in ramp]) for c in range(3)]
    return Image.merge("RGBA", (*channels, Image.new("L", source.size, 255)))


@lru_cache(maxsize=5)
def placeholder_object(kind: str) -> Image.Image:
    """An isolated RGBA cabinet for a catalog with no reachable artwork."""
    if kind not in _PLACEHOLDER_KINDS:
        raise ValueError(f"Unknown catalog placeholder: {kind}")
    with Image.open(_ART_DIRECTORY / f"placeholder-{kind}-v2.png") as image:
        return image.convert("RGBA")


def _place_placeholder(
    base: Image.Image, kind: str, *, thumbnail: bool = False
) -> None:
    artwork = placeholder_object(kind)
    bounds = artwork.getchannel("A").getbbox()
    if bounds is None:
        return
    artwork = ImageOps.contain(
        artwork.crop(bounds),
        (
            round(base.width * (0.90 if thumbnail else 0.28)),
            round(base.height * (0.88 if thumbnail else 0.62)),
        ),
        Image.Resampling.LANCZOS,
    )
    center_x = base.width * (0.5 if thumbnail else 0.82)
    center_y = base.height * (0.51 if thumbnail else 0.55)
    base.alpha_composite(
        artwork,
        (round(center_x - artwork.width / 2), round(center_y - artwork.height / 2)),
    )


def _background(
    width: int, height: int, text_key: str, *, square: bool = False
) -> Image.Image:
    source = _backdrop(background_design(text_key))
    if square:
        source = ImageOps.fit(
            source, (width, height), Image.Resampling.LANCZOS, centering=(0.8, 0.5)
        )
    else:
        source = source.resize((width, height), Image.Resampling.LANCZOS)
    return _tint_background(source, text_key)


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
    layout: Sequence[tuple[float, float, float]],
    cover_fraction: float,
) -> None:
    side = round(height * cover_fraction)
    radius = round(side * 0.055)
    picks = list(covers[: len(layout)])
    for idx in reversed(range(len(picks))):  # back to front
        cx_f, cy_f, angle = layout[idx]
        tile = _rounded_tile(picks[idx], side, radius)
        _paste_with_shadow(
            base, tile, (round(width * cx_f), round(height * cy_f)), angle
        )


def _grain(
    image: Image.Image, rng: np.random.Generator, strength: float
) -> Image.Image:
    arr = np.asarray(image, dtype=np.float32)
    noise = (
        rng.standard_normal((image.height, image.width, 1)).astype(np.float32)
        * strength
    )
    return Image.fromarray(np.clip(arr + noise, 0, 255).astype("uint8"), "RGB")


def render_catalog_art(
    covers: Sequence[Image.Image],
    names: Sequence[str],
    seed: str,
    *,
    width: int = CANVAS_W,
    height: int = CANVAS_H,
    placeholder: str | None = None,
) -> Image.Image:
    """Render a Kalinka card with real covers, or a cabinet when none exist.
    The first nonempty name selects the geometry and its tint."""
    rng = _rng(seed)
    ss = _SUPERSAMPLE
    base = _background(width * ss, height * ss, background_text_key(names, seed))
    picks = list(covers[:3])
    if picks:
        _collage(
            base,
            picks,
            width * ss,
            height * ss,
            _CARD_LAYOUTS[len(picks)],
            _COVER_FRACTION,
        )
    elif placeholder:
        _place_placeholder(base, placeholder)
    out = base.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    return _grain(out, rng, _GRAIN)


def render_catalog_thumbnail(
    covers: Sequence[Image.Image],
    seed: str,
    *,
    side: int = THUMBNAIL_SIDE,
    placeholder: str | None = None,
    names: Sequence[str] = (),
) -> Image.Image:
    """The card's art, square: its background cropped around the artwork side,
    and up to three covers gathered to the middle."""
    rng = _rng(seed)
    ss = _SUPERSAMPLE
    base = _background(
        side * ss, side * ss, background_text_key(names, seed), square=True
    )
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
    elif placeholder:
        _place_placeholder(base, placeholder, thumbnail=True)
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
    *,
    placeholder: str | None = None,
) -> str:
    """Content identity (style version + inputs); unchanged -> cache is current."""
    hasher = hashlib.sha1()
    hasher.update(f"style:{STYLE_VERSION}".encode())
    hasher.update(f"shape:{style.value}".encode())
    hasher.update(f"seed:{seed}".encode())
    if placeholder and not cover_bytes:
        hasher.update(f"placeholder:{placeholder}".encode())
    for blob in cover_bytes:
        hasher.update(b"cover:")
        hasher.update(hashlib.sha1(blob).digest())
    hasher.update(f"text:{background_text_key(names, seed)}".encode())
    return hasher.hexdigest()
