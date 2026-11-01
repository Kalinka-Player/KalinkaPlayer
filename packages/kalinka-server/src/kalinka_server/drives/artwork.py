"""Covers of files on drives, made on request and kept in a bounded cache."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

from PIL import Image

from kalinka_plugin_sdk.datamodel import CoverImage
from kalinka_plugin_sdk.media.folder_art import LocalFiles, find_folder_cover
from kalinka_plugin_sdk.media.images import ARTWORK_SIZES, resize_to_jpeg
from kalinka_plugin_sdk.media.tags import read_cover

from .ids import SOURCE, decode_path, encode_path
from .listing import list_folder

logger = logging.getLogger(__name__.split(".")[-1])

#: A file's own cover, and a folder's: its images, else its first track's.
EMBEDDED = "e"
FOLDER = "f"

_RESOURCE = re.compile(rf"^{SOURCE}/([{EMBEDDED}{FOLDER}])\.([A-Za-z0-9_-]+)_([a-z]+)\.jpg$")
DEFAULT_CAPACITY_BYTES = 256 * 1024 * 1024
_EVICT_EVERY = 64
#: A cover kept is touched for its place in the eviction order at most this
#: often, as every touch is a write to the SD card.
_TOUCH_EVERY_S = 3600.0
#: How long a file or folder found to have no cover is not searched again.
_NONE_FOR_S = 600.0
_NONE_KEPT = 2000
#: A partial cover older than this was left by a write that failed.
_STALE_PART_S = 600.0


def cover_urls(kind: str, path: str) -> CoverImage:
    base = f"/resource/{SOURCE}/{kind}.{encode_path(path)}"
    return CoverImage(
        thumbnail=f"{base}_thumbnail.jpg",
        small=f"{base}_small.jpg",
        large=f"{base}_large.jpg",
    )


def _cropped(data: bytes, box) -> Optional[bytes]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            w, h = image.size
            panel = image.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
            out = io.BytesIO()
            panel.convert("RGB").save(out, format="PNG")
            return out.getvalue()
    except (OSError, ValueError):
        return None


def _folder_cover(folder: str, allowed: Callable[[str], bool]) -> Optional[bytes]:
    found = find_folder_cover(LocalFiles(), folder)
    # An image linked from elsewhere, or found through a linked folder, is not
    # this drive's to show.
    if found is not None and allowed(os.path.realpath(found.path)):
        try:
            with open(found.path, "rb") as f:
                data = f.read()
        except OSError:
            data = None
        if data is not None:
            return _cropped(data, found.box) if found.box else data
    try:
        contents = list_folder(folder)
    except OSError:
        return None
    for track in contents.tracks[:3]:
        try:
            cover = read_cover(track.path)
        except OSError:
            continue
        if cover:
            return cover
    return None


class DriveArtwork:
    """Answers ``/resource/drives/…`` with a JPEG made from a file or folder.

    @param allowed Whether a path may be read: it must be on a volume the
        storage manager knows, so a crafted resource name reads nothing else.
    @note Thread-safe; :meth:`path_of` blocks on the drive and belongs on a
        worker thread.
    """

    def __init__(
        self,
        cache_dir: str,
        allowed: Callable[[str], bool],
        capacity_bytes: int = DEFAULT_CAPACITY_BYTES,
    ):
        self._dir = cache_dir
        self._allowed = allowed
        self._capacity = capacity_bytes
        self._lock = threading.Lock()
        self._writes = 0
        self._none: OrderedDict[str, float] = OrderedDict()

    def path_of(self, resource: str) -> Optional[str]:
        match = _RESOURCE.match(resource)
        if match is None:
            return None
        kind, token, size_name = match.groups()
        px = ARTWORK_SIZES.get(size_name)
        path = decode_path(token)
        if px is None or path is None:
            return None
        real = os.path.realpath(path)
        if not self._allowed(real):
            return None
        try:
            st = os.stat(real)
        except OSError:
            return None
        key = hashlib.sha1(f"{kind}:{real}:{st.st_size}:{st.st_mtime_ns}:{px}".encode()).hexdigest()
        cached = os.path.join(self._dir, f"{key}.jpg")
        try:
            kept = os.stat(cached)
            if time.time() - kept.st_mtime > _TOUCH_EVERY_S:
                os.utime(cached)
            return cached
        except OSError:
            pass
        if self._known_empty(key):
            return None
        try:
            source = read_cover(real) if kind == EMBEDDED else _folder_cover(real, self._allowed)
        except OSError:
            return None
        jpeg = resize_to_jpeg(source, px) if source else None
        if jpeg is None:
            self._remember_empty(key)
            return None
        partial = f"{cached}.{threading.get_ident()}.part"
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(partial, "wb") as f:
                f.write(jpeg)
            os.replace(partial, cached)
        except OSError as e:
            logger.warning("Could not keep a cover: %s", e)
            try:
                os.unlink(partial)
            except OSError:
                pass
            return None
        self._maybe_evict()
        return cached

    def _known_empty(self, key: str) -> bool:
        with self._lock:
            found = self._none.get(key)
            if found is not None and time.monotonic() - found < _NONE_FOR_S:
                return True
            self._none.pop(key, None)
        return False

    def _remember_empty(self, key: str) -> None:
        with self._lock:
            self._none[key] = time.monotonic()
            self._none.move_to_end(key)
            while len(self._none) > _NONE_KEPT:
                self._none.popitem(last=False)

    def _maybe_evict(self) -> None:
        with self._lock:
            self._writes += 1
            if self._writes % _EVICT_EVERY:
                return
        files = []
        now = time.time()
        try:
            with os.scandir(self._dir) as entries:
                for e in entries:
                    try:
                        st = e.stat()
                        if e.name.endswith(".jpg"):
                            files.append((st.st_mtime, st.st_size, e.path))
                        elif e.name.endswith(".part") and now - st.st_mtime > _STALE_PART_S:
                            os.unlink(e.path)
                    except OSError:
                        continue
        except OSError:
            return
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self._capacity:
                break
            try:
                os.unlink(path)
                total -= size
            except OSError:
                pass
