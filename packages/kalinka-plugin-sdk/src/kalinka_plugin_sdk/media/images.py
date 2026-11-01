"""Turning cover art into the JPEG sizes clients ask for."""

import io
from typing import Optional

from PIL import Image

#: The pixel sizes clients name their artwork by.
ARTWORK_SIZES = {"thumbnail": 50, "small": 230, "large": 600}


def resize_to_jpeg(data: bytes, max_side: int, quality: int = 85) -> Optional[bytes]:
    """``data`` decoded and fitted within ``max_side`` pixels, as JPEG, or
    None when it is not an image Pillow can read."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=quality)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
