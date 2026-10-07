"""A folder's cover, made from the covers found under it and composed the
first time it is asked for.

A composed cover's link names the folder and the four covers it is made of,
and it is composed only for a folder in the library whose first covers are
still those four. However a link is spelled, a request can cost no more than
one mosaic per folder; and composing a folder's mosaic for new covers removes
the one it had before, so a folder keeps one on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from itertools import islice
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

from kalinka_plugin_sdk.datamodel import CoverImage

from ..folder_ids import decode_folder_id, encode_folder_id
from .artwork_store import ArtworkSave, artwork_file, served_cover, store_decoded_image
from .cover_collage import MOSAIC_TILES, compose_cover

FOLDER_ART = "folder"

#: A cover as listed: ``(kind, image id)``, the kind being album or track.
Cover = Tuple[str, str]

_COVER_ID = re.compile(r"(album|track)_([0-9a-f]{16})")
_TILE = r"[at][0-9a-f]{16}"
_KEY = rf"(?:{_TILE}-){{{MOSAIC_TILES - 1}}}{_TILE}"
_COMPOSED = re.compile(
    rf"{FOLDER_ART}/([A-Za-z0-9_-]+)/({_KEY})_(thumbnail|small|large)\.jpg"
)


def _key_of(covers: Sequence[Cover]) -> str:
    return "-".join(kind[0] + image_id.split("_", 1)[1] for kind, image_id in covers)


class FolderCovers:
    """Turns the covers found under a folder into the folder's own, and
    serves the ones it composed.

    Checking a link and composing its mosaic run off the event loop. Two
    requests composing one cover at once write the same bytes, each size
    moved into place whole.
    """

    def __init__(
        self,
        artwork_path: Union[str, os.PathLike],
        covers_of: Callable[[str], Optional[Sequence[Cover]]],
    ):
        """@param covers_of A folder's covers in path order, as listed; None
        for a folder that is not in the library. Called from a worker
        thread."""
        self._artwork_path = Path(artwork_path)
        self._covers_of = covers_of

    def cover_of(self, folder: str, covers: Sequence[Cover]) -> Optional[CoverImage]:
        """The image of ``folder``, whose covers in path order are
        ``covers``: a composed mosaic of the first four that exist, the first
        alone when fewer do, None when none does."""
        usable = self._usable(covers)
        if not usable:
            return None
        if len(usable) < MOSAIC_TILES:
            return served_cover(*usable[0])
        return served_cover(f"{FOLDER_ART}/{encode_folder_id(folder)}", _key_of(usable))

    async def path_of(self, resource: str) -> Optional[str]:
        """The file behind a composed cover's ``resource`` name, composing it
        first if it has not been; None unless the name is the one
        :meth:`cover_of` gives the folder it names now."""
        match = _COMPOSED.fullmatch(resource)
        if match is None:
            return None
        encoded, key, size = match.groups()
        folder = decode_folder_id(encoded)
        if folder is None:
            return None
        store = self._store_of(folder)
        target = artwork_file(self._artwork_path, store, key, size)
        if target.exists():
            return str(target)
        saved = await asyncio.to_thread(self._compose, folder, store, key)
        return str(target) if saved is ArtworkSave.SAVED else None

    def _usable(self, covers: Sequence[Cover]) -> List[Cover]:
        return list(
            islice((cover for cover in covers if self._exists(*cover)), MOSAIC_TILES)
        )

    def _exists(self, kind: str, image_id: str) -> bool:
        match = _COVER_ID.fullmatch(image_id)
        return (
            match is not None
            and match[1] == kind
            and artwork_file(self._artwork_path, kind, image_id, "large").exists()
        )

    @staticmethod
    def _store_of(folder: str) -> str:
        # A hash, since a folder's id can be longer than a file name may be.
        digest = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:16]
        return f"{FOLDER_ART}/{digest}"

    def _compose(self, folder: str, store: str, key: str) -> Optional[ArtworkSave]:
        covers = self._covers_of(folder)
        if covers is None:
            return None
        usable = self._usable(covers)
        if len(usable) < MOSAIC_TILES or _key_of(usable) != key:
            return None
        cover = compose_cover(
            [artwork_file(self._artwork_path, *tile, "large") for tile in usable]
        )
        if cover is None:
            return ArtworkSave.UNDECODABLE
        saved = store_decoded_image(cover, self._artwork_path, key, store)
        if saved is ArtworkSave.SAVED:
            self._forget_others(store, key)
        return saved

    def _forget_others(self, store: str, key: str) -> None:
        """Remove the mosaics the folder had for covers it no longer leads
        with."""
        for stale in (self._artwork_path / store).glob("*.jpg"):
            if not stale.name.startswith(f"{key}_"):
                stale.unlink(missing_ok=True)
