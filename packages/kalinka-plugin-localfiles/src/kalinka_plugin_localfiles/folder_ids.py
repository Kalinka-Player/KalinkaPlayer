"""A folder's id: its canonical location, reversibly, in characters an entity
id may hold.

URL-safe base64 has neither ``:``, which separates an entity id's parts, nor
``/``, which the browse route would read as a path step.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Optional

_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")


def encode_folder_id(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def decode_folder_id(folder_id: str) -> Optional[str]:
    """The location ``folder_id`` names, or None when it names none."""
    if not _ALPHABET.fullmatch(folder_id) or len(folder_id) % 4 == 1:
        return None
    padded = folder_id + "=" * (-len(folder_id) % 4)
    try:
        path = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return path if path and "\0" not in path else None
