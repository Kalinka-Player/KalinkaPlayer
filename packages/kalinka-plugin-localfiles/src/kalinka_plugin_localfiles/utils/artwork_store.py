"""Artwork files on disk, in the three sizes every consumer expects.

One writer for the downloaded-cover layout (``<artwork>/<type>/<id>_<size>.jpg``)
shared by the indexer's embedded-art extraction and every enrichment source
that fetches covers, so the sizes and naming cannot drift apart.

Two ways in, because callers hold covers in two forms: a source that fetched
one over HTTP has bytes, while a sleeve scan found beside the audio is a file
— and reading that as it decodes lets a large one be decoded without first
being held in memory whole.
"""

from __future__ import annotations

import io
import logging
import os
import threading
from enum import Enum, auto
from pathlib import Path
from typing import BinaryIO, Optional, Tuple, Union

from kalinka_plugin_sdk.datamodel import CoverImage
from PIL import Image

from .watched_file import WatchedFile

logger = logging.getLogger(__name__.split(".")[-1])

_SIZES = (("thumbnail", 50), ("small", 230), ("large", 600))
_LARGEST = max(size for _, size in _SIZES)


def artwork_file(
    artwork_path: Union[str, os.PathLike], entity_type: str, image_id: str, size: str
) -> Path:
    """Where one size of a stored image set is."""
    return Path(artwork_path) / entity_type / f"{image_id}_{size}.jpg"


def served_cover(entity_type: str, image_id: str) -> CoverImage:
    """The URLs the server answers a stored image set at, through the
    module's ``get_resource_path``."""
    base = f"/resource/{entity_type}/{image_id}"
    return CoverImage(
        thumbnail=f"{base}_thumbnail.jpg",
        small=f"{base}_small.jpg",
        large=f"{base}_large.jpg",
    )


class ArtworkSave(Enum):
    """How storing a cover went, for a caller that remembers failures."""

    SAVED = auto()
    #: The picture itself is broken: the same bytes will fail the same way.
    UNDECODABLE = auto()
    #: The source could not be read or the artwork could not be written,
    #: which says nothing about the picture.
    IO_FAILED = auto()


def save_artwork_images(
    artwork_path: Union[str, os.PathLike],
    image_data: bytes,
    entity_id: str,
    entity_type: str,
    origin: Optional[str] = None,
) -> bool:
    """Decode ``image_data`` and save it as thumbnail/small/large JPEGs.

    Returns False (and logs) on any failure — a broken image must not fail
    the enrichment or indexing pass that found it.

    @param origin Where the image came from, named in the failure log so
        the broken file can be found; only the entity is named without it.
    """
    outcome = store_artwork_images(
        artwork_path, image_data, entity_id, entity_type, origin
    )
    return outcome is ArtworkSave.SAVED


def store_artwork_images(
    artwork_path: Union[str, os.PathLike],
    image_data: bytes,
    entity_id: str,
    entity_type: str,
    origin: Optional[str] = None,
) -> ArtworkSave:
    """As :func:`save_artwork_images`, saying why nothing was saved."""
    try:
        with Image.open(io.BytesIO(image_data)) as img:
            decoded = _decoded(img, None)
            return store_decoded_image(
                decoded, artwork_path, entity_id, entity_type, origin
            )
    except Exception as e:  # noqa: BLE001 - reported as the outcome
        _log_failure(entity_type, entity_id, origin, e)
        return ArtworkSave.UNDECODABLE


def store_artwork_from_file(
    artwork_path: Union[str, os.PathLike],
    source: BinaryIO,
    entity_id: str,
    entity_type: str,
    box: Optional[Tuple[float, float, float, float]] = None,
    origin: Optional[str] = None,
) -> ArtworkSave:
    """As :func:`store_artwork_images`, for a cover that is already a file.

    @param source The image as an open binary file — which is how a cover on
        a share arrives, since only its storage can read it. A read that
        fails part-way is told apart from a picture that will not decode.

    A sleeve scan can be far larger than anything downloaded, so the JPEG
    decoder is asked for a reduced scale up front: nothing here needs more
    than the largest stored size, and a 3000px scan then costs a sixteenth
    of the memory. ``draft`` is a no-op for formats that cannot do it.

    @param box The part of the image to keep, as ``(left, top, right,
        bottom)`` fractions — the whole image when omitted. Fractions
        because ``draft`` has already changed what the pixels measure.
    @param origin As for :func:`save_artwork_images`; an open file cannot
        say where it came from.
    """
    watched = WatchedFile(source)
    try:
        with Image.open(watched) as img:
            img.draft("RGB", (_LARGEST, _LARGEST))
            decoded = _decoded(img, box)
            return store_decoded_image(
                decoded, artwork_path, entity_id, entity_type, origin
            )
    except Exception as e:  # noqa: BLE001 - reported as the outcome
        _log_failure(entity_type, entity_id, origin, e)
        if watched.read_error is not None:
            return ArtworkSave.IO_FAILED
        return ArtworkSave.UNDECODABLE


def _log_failure(
    entity_type: str, entity_id: str, origin: Optional[str], error: Exception
) -> None:
    source = f" from {origin}" if origin else ""
    logger.error(
        f"Error saving artwork for {entity_type} {entity_id}{source}: {error}"
    )


def _decoded(
    img: Image.Image, box: Optional[Tuple[float, float, float, float]]
) -> Image.Image:
    """``img`` decoded in full, cut to ``box`` and in RGB. Raises whatever
    the picture's decoder does."""
    img.load()
    img = _cropped(img, box)
    return img if img.mode == "RGB" else img.convert("RGB")


def _cropped(
    img: Image.Image, box: Optional[Tuple[float, float, float, float]]
) -> Image.Image:
    """``img`` reduced to ``box``, or unchanged when there is nothing to cut."""
    if not box:
        return img
    width, height = img.size
    left, top, right, bottom = box
    return img.crop(
        (
            int(left * width),
            int(top * height),
            int(right * width),
            int(bottom * height),
        )
    )


def store_decoded_image(
    img: Image.Image,
    artwork_path: Union[str, os.PathLike],
    entity_id: str,
    entity_type: str,
    origin: Optional[str] = None,
) -> ArtworkSave:
    """Write one decoded RGB image out in every size. Never raises: the
    picture is already decoded, so a failure here is the artwork
    directory's.

    Each size is written beside its name and moved into place, so a reader
    never sees one half written — the server sending a cover composed a
    moment ago included.
    """
    try:
        dir_path = os.path.join(artwork_path, entity_type)
        os.makedirs(dir_path, exist_ok=True)
        for suffix, size in _SIZES:
            copy = img.copy()
            copy.thumbnail((size, size), Image.Resampling.LANCZOS)
            _write_jpeg(copy, os.path.join(dir_path, f"{entity_id}_{suffix}.jpg"))
    except Exception as e:  # noqa: BLE001 - reported as the outcome
        _log_failure(entity_type, entity_id, origin, e)
        return ArtworkSave.IO_FAILED
    return ArtworkSave.SAVED


def _write_jpeg(img: Image.Image, target: str) -> None:
    partial = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        img.save(partial, "JPEG", quality=90)
        os.replace(partial, target)
    except BaseException:
        if os.path.exists(partial):
            os.unlink(partial)
        raise
