"""Catalog cards tint a grayscale backdrop before adding up to three album covers.

The app overlays labels on the dark left column; square thumbnails gather the
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
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageOps

STYLE_VERSION = 11

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
BACKGROUND_DESIGNS = ("sweep", "arch", "diagonal", "circles", "wave", "block")
BACKGROUND_PALETTES = (
    ("#A13B50", "#CE8291"),
    ("#AF563D", "#DFA080"),
    ("#94702F", "#CFB66B"),
    ("#687B38", "#A8BB77"),
    ("#2F7958", "#82B499"),
    ("#28836D", "#82C4AE"),
    ("#277B84", "#81B7C4"),
    ("#367F9E", "#8BC4D9"),
    ("#32669B", "#82A9D4"),
    ("#4E5CA6", "#A1A8DC"),
    ("#6A4E9B", "#B59FD5"),
    ("#834D91", "#C59CCB"),
    ("#A34479", "#D894BA"),
    ("#A54F67", "#DF9BA8"),
    ("#896456", "#C3AA90"),
    ("#526F7D", "#ADC3C9"),
)
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


@dataclass(frozen=True)
class BackgroundTransform:
    """Title-selected geometry around the artwork side, before the caption fade."""

    rotation: float = 0
    offset_x: float = 0
    offset_y: float = 0
    scale_x: float = 1
    scale_y: float = 1

    def affine(self, width: float, height: float) -> tuple[float, ...]:
        """Forward affine coefficients: a, b, c, d, tx, ty."""
        angle = math.radians(self.rotation)
        cosine, sine = math.cos(angle), math.sin(angle)
        a, b = cosine * self.scale_x, -sine * self.scale_y
        c, d = sine * self.scale_x, cosine * self.scale_y
        px, py = 0.82 * width, 0.5 * height
        return (
            a,
            b,
            c,
            d,
            px + self.offset_x * width - a * px - b * py,
            py + self.offset_y * height - c * px - d * py,
        )


def background_transform(text_key: str) -> BackgroundTransform:
    """Use independent hash bytes so a shared shape and palette can still vary."""
    values = bytes.fromhex(text_key)[24:29]
    return BackgroundTransform(
        rotation=values[0] % 61 - 30,
        offset_x=(values[1] / 255 - 0.5) * 0.14,
        offset_y=(values[2] / 255 - 0.5) * 0.50,
        scale_x=0.82 + values[3] / 255 * 0.36,
        scale_y=0.80 + values[4] / 255 * 0.40,
    )


@lru_cache(maxsize=len(BACKGROUND_DESIGNS))
def _backdrop(
    variant: int, transform: BackgroundTransform = BackgroundTransform()
) -> Image.Image:
    width, height = CANVAS_W * _SUPERSAMPLE, CANVAS_H * _SUPERSAMPLE
    source = Image.new("L", (width, height), 48)
    draw = ImageDraw.Draw(source)
    a, b, c, d, tx, ty = transform.affine(width, height)

    def polygon(points: Sequence[tuple[float, float]], fill: int) -> None:
        draw.polygon(
            [(a * x + b * y + tx, c * x + d * y + ty) for x, y in points],
            fill=fill,
        )

    def ellipse(bounds: tuple[float, ...], fill: int) -> None:
        left, top, right, bottom = bounds
        rx, ry = (right - left) / 2, (bottom - top) / 2
        cx, cy = left + rx, top + ry
        polygon(
            [
                (cx + rx * math.cos(t), cy + ry * math.sin(t))
                for t in np.linspace(0, 2 * math.pi, 192, endpoint=False)
            ],
            fill,
        )

    def rounded_rectangle(
        bounds: tuple[float, ...], radius: float
    ) -> list[tuple[float, float]]:
        left, top, right, bottom = bounds
        radius = min(radius, (right - left) / 2, (bottom - top) / 2)
        points = []
        for cx, cy, start in (
            (right - radius, top + radius, -math.pi / 2),
            (right - radius, bottom - radius, 0),
            (left + radius, bottom - radius, math.pi / 2),
            (left + radius, top + radius, math.pi),
        ):
            points.extend(
                (cx + radius * math.cos(t), cy + radius * math.sin(t))
                for t in np.linspace(start, start + math.pi / 2, 49)
            )
        return points

    # Transform complete paths before clipping; rotating a cropped bitmap exposes seams.
    design = BACKGROUND_DESIGNS[variant]
    if design == "sweep":
        ellipse((0.54 * width, -0.85 * height, 1.32 * width, 1.3 * height), 176)
        ellipse((0.64 * width, -0.94 * height, 1.34 * width, 0.97 * height), 92)
    elif design == "arch":
        polygon(
            rounded_rectangle(
                (0.59 * width, 0.08 * height, 1.04 * width, 1.8 * height),
                0.225 * width,
            ),
            192,
        )
        polygon(
            rounded_rectangle(
                (0.68 * width, 0.36 * height, 0.95 * width, 1.8 * height),
                0.135 * width,
            ),
            92,
        )
    elif design == "diagonal":
        polygon(
            [
                (0.53 * width, height),
                (0.80 * width, 0),
                (width, 0),
                (0.73 * width, height),
            ],
            fill=184,
        )
    elif design == "circles":
        radius = 0.62 * height
        cx, cy = 0.82 * width, 0.36 * height
        ellipse((cx - radius, cy - radius, cx + radius, cy + radius), 184)
        radius = 0.24 * height
        cx, cy = 0.96 * width, 0.92 * height
        ellipse((cx - radius, cy - radius, cx + radius, cy + radius), 112)
    elif design == "wave":
        xs = np.linspace(0.45 * width, width, 240)
        ys = height * (0.52 + 0.22 * np.sin((xs / width - 0.45) * 3 * np.pi))
        polygon(list(zip(xs, ys)) + [(width, height), (0.45 * width, height)], fill=184)
    elif design == "block":
        # Preserve the original rotated tile's centre and tilt.
        tile = rounded_rectangle((-480, -580, 479, 579), 0.13 * height)
        cosine, sine = math.cos(math.radians(24)), math.sin(math.radians(24))
        polygon(
            [
                (cosine * x + sine * y + 2091, -sine * x + cosine * y + 406)
                for x, y in tile
            ],
            176,
        )
    return source


def _background_colors(text_key: str) -> tuple[str, str]:
    accent, highlight = BACKGROUND_PALETTES[
        int(text_key[16:32], 16) % len(BACKGROUND_PALETTES)
    ]
    strength = (0.90, 1.0, 1.10)[int(text_key[32:48], 16) % 3]
    rgb = tuple(min(240, round(c * strength)) for c in ImageColor.getrgb(highlight))
    return accent, "#{:02X}{:02X}{:02X}".format(*rgb)


def _tint_background(source: Image.Image, text_key: str) -> Image.Image:
    accent, highlight = _background_colors(text_key)
    shadow = tuple(round(c * 0.35) for c in ImageColor.getrgb(accent))
    return ImageOps.colorize(
        source, black=shadow, mid=accent, white=highlight, midpoint=160
    )


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
    width: int, height: int, text_key: str, *, text_column: bool = True
) -> Image.Image:
    source = _backdrop(
        int(text_key[:16], 16) % len(BACKGROUND_DESIGNS),
        background_transform(text_key),
    )
    if text_column:
        source = source.resize((width, height), Image.Resampling.LANCZOS)
        fade = np.clip((np.linspace(0, 1, width) - 0.58) / 0.14, 0, 1)
        alpha = (fade * fade * (3 - 2 * fade) * 255).astype("uint8")
        source = Image.composite(
            source,
            Image.new("L", source.size, 48),
            Image.fromarray(alpha[None, :]).resize(source.size),
        )
    else:
        source = ImageOps.fit(
            source, (width, height), Image.Resampling.LANCZOS, centering=(0.8, 0.5)
        )
    return _tint_background(source, text_key).convert("RGBA")


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
    The first nonempty name selects the shape and both tonal colours."""
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
    """The card's art, square: its background without the text column, and up
    to three covers gathered to the middle."""
    rng = _rng(seed)
    ss = _SUPERSAMPLE
    base = _background(
        side * ss, side * ss, background_text_key(names, seed), text_column=False
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
