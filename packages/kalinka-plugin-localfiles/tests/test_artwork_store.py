#!/usr/bin/env python3
"""Why a cover could not be stored, and which file it came from.

A caller that remembers failed covers must tell a picture that is broken —
which fails the same way every time — from a read or a write that failed,
which says nothing about the picture. And the entity id alone does not say
which file is broken, which is what the user has to replace.
"""

import io
import logging

import pytest
from PIL import Image

from kalinka_plugin_localfiles.utils.artwork_store import (
    ArtworkSave,
    save_artwork_images,
    store_artwork_from_file,
    store_artwork_images,
)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.effect_noise((400, 400), 64).convert("RGB").save(buf, "JPEG")
    return buf.getvalue()


def _truncated_jpeg() -> bytes:
    data = _jpeg()
    return data[: len(data) // 2]


class _DroppingFile(io.BytesIO):
    """A file on a share that stops answering half-way through."""

    def read(self, size=-1):
        end = len(self.getbuffer()) if size is None or size < 0 else self.tell() + size
        if end > len(self.getbuffer()) // 2:
            raise OSError("host is down")
        return super().read(size)


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "GIF"])
def test_a_cover_is_stored_in_every_format_a_folder_offers(tmp_path, image_format):
    """The stream is watched, not handed over, so each decoder has to find
    everything it reads through on it."""
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (30, 90, 140)).save(buf, image_format)
    buf.seek(0)

    outcome = store_artwork_from_file(tmp_path, buf, "album_1", "album")

    assert outcome is ArtworkSave.SAVED
    assert (tmp_path / "album" / "album_1_large.jpg").exists()


def test_a_file_that_will_not_decode_is_named(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        outcome = store_artwork_from_file(
            tmp_path / "artwork",
            io.BytesIO(_truncated_jpeg()),
            "album_1",
            "album",
            origin="/music/X/Front.jpg",
        )

    assert outcome is ArtworkSave.UNDECODABLE
    [record] = caplog.records
    assert "album album_1 from /music/X/Front.jpg: " in record.getMessage()
    assert "truncated" in record.getMessage()


def test_a_read_that_fails_part_way_is_not_a_broken_picture(tmp_path):
    outcome = store_artwork_from_file(
        tmp_path / "artwork", _DroppingFile(_jpeg()), "album_1", "album"
    )

    assert outcome is ArtworkSave.IO_FAILED


def test_an_artwork_directory_that_cannot_be_written_is_not_a_broken_picture(
    tmp_path,
):
    blocked = tmp_path / "artwork"
    blocked.write_text("a file where the directory should be")

    from_file = store_artwork_from_file(
        blocked, io.BytesIO(_jpeg()), "album_1", "album"
    )
    from_bytes = store_artwork_images(blocked, _jpeg(), "album_1", "album")

    assert from_file is ArtworkSave.IO_FAILED
    assert from_bytes is ArtworkSave.IO_FAILED
    assert save_artwork_images(blocked, _jpeg(), "album_1", "album") is False


def test_bytes_that_will_not_decode_are_named(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        outcome = store_artwork_images(
            tmp_path / "artwork",
            b"not an image",
            "track_1",
            "track",
            origin="/music/loose.flac",
        )

    assert outcome is ArtworkSave.UNDECODABLE
    [record] = caplog.records
    assert "track track_1 from /music/loose.flac: " in record.getMessage()


def test_without_an_origin_only_the_entity_is_named(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert not save_artwork_images(
            tmp_path / "artwork", b"not an image", "album_1", "album"
        )

    [record] = caplog.records
    assert "Error saving artwork for album album_1: " in record.getMessage()
