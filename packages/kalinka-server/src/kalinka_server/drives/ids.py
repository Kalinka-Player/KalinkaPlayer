"""Drives' entity ids: a folder or a track is named by its path."""

from __future__ import annotations

import base64
from typing import Optional

from kalinka_plugin_sdk.datamodel import EntityId, EntityType

SOURCE = "drives"
ROOT = "root"
VOLUME_PREFIX = "volume."
FOLDER_PREFIX = "folder."


def encode_path(path: str) -> str:
    raw = path.encode("utf-8", "surrogateescape")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_path(token: str) -> Optional[str]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        path = raw.decode("utf-8", "surrogateescape")
    except (ValueError, UnicodeDecodeError):
        return None
    return path if path.startswith("/") and "\0" not in path else None


def catalog_id(endpoint: str) -> EntityId:
    return EntityId(id=endpoint, type=EntityType.CATALOG, source=SOURCE)


def volume_id(volume: str) -> EntityId:
    return catalog_id(VOLUME_PREFIX + volume)


def folder_id(path: str) -> EntityId:
    return catalog_id(FOLDER_PREFIX + encode_path(path))


def track_id(path: str) -> EntityId:
    return EntityId(id=encode_path(path), type=EntityType.TRACK, source=SOURCE)


def album_id(folder: str) -> EntityId:
    return EntityId(id=encode_path(folder), type=EntityType.ALBUM, source=SOURCE)


def artist_id(name: str) -> EntityId:
    return EntityId(id=encode_path(name), type=EntityType.ARTIST, source=SOURCE)
