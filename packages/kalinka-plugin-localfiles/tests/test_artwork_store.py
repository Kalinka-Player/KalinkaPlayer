#!/usr/bin/env python3
"""A cover that will not decode is reported with the file it came from.

The entity id alone does not say which of an album folder's images, or which
track's embedded picture, is broken — and that file is what the user has to
replace.
"""

import io
import logging

from PIL import Image

from kalinka_plugin_localfiles.utils.artwork_store import (
    save_artwork_from_file,
    save_artwork_images,
)


def _truncated_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.effect_noise((400, 400), 64).convert("RGB").save(buf, "JPEG")
    return buf.getvalue()[: buf.tell() // 2]


def test_a_file_that_will_not_decode_is_named(tmp_path, caplog):
    cover = tmp_path / "Front.jpg"
    cover.write_bytes(_truncated_jpeg())

    with caplog.at_level(logging.ERROR), open(cover, "rb") as handle:
        saved = save_artwork_from_file(
            tmp_path / "artwork", handle, "album_1", "album", origin=str(cover)
        )

    assert saved is False
    [record] = caplog.records
    assert f"album album_1 from {cover}: " in record.getMessage()
    assert "truncated" in record.getMessage()


def test_bytes_that_will_not_decode_are_named(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        saved = save_artwork_images(
            tmp_path / "artwork",
            b"not an image",
            "track_1",
            "track",
            origin="/music/loose.flac",
        )

    assert saved is False
    [record] = caplog.records
    assert "track track_1 from /music/loose.flac: " in record.getMessage()


def test_without_an_origin_only_the_entity_is_named(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert not save_artwork_images(
            tmp_path / "artwork", b"not an image", "album_1", "album"
        )

    [record] = caplog.records
    assert "Error saving artwork for album album_1: " in record.getMessage()
