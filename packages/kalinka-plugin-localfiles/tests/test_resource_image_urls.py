#!/usr/bin/env python3
"""The ``/resource/`` links a browse item carries for a saved image set.

A link names a path, and its query says when the set was written: a cover
replaced under the same name gets a new link, so a client caching by link
shows the new picture. Whatever resolves a link as a file reads the path
before the query, as the server and its catalog-art composer do.
"""

import os


import pytest
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule


class _FakeDb:
    def is_good(self):
        return True


@pytest.fixture
def module(tmp_path):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path / "music")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return LocalFilesInputModule(config, _FakeDb())


def _path(url):
    return url.split("?", 1)[0]


def _write_art(module, entity_type, base):
    folder = module.artwork_path / entity_type
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("thumbnail", "small", "large"):
        Image.new("RGB", (10, 10), (4, 5, 6)).save(
            folder / f"{base}_{suffix}.jpg", "JPEG"
        )


class TestTheLink:
    def test_every_size_is_offered(self, module):
        _write_art(module, "album", "album_1")
        urls = module._resource_image_urls("album", "album_1.jpg")

        assert [_path(url) for url in (urls.thumbnail, urls.small, urls.large)] == [
            "/resource/album/album_1_thumbnail.jpg",
            "/resource/album/album_1_small.jpg",
            "/resource/album/album_1_large.jpg",
        ]

    def test_a_cover_replaced_under_its_name_gets_a_new_link(self, module):
        _write_art(module, "album", "album_1")
        before = module._resource_image_urls("album", "album_1.jpg")
        large = module.artwork_path / "album" / "album_1_large.jpg"
        later = large.stat().st_mtime_ns + 1_000_000_000
        os.utime(large, ns=(later, later))

        after = module._resource_image_urls("album", "album_1.jpg")

        assert _path(after.small) == _path(before.small)
        assert after.small != before.small

    def test_the_path_names_a_file_that_exists(self, module):
        _write_art(module, "album", "album_1")
        urls = module._resource_image_urls("album", "album_1.jpg")

        assert (
            module.artwork_path / _path(urls.thumbnail).removeprefix("/resource/")
        ).is_file()

    @pytest.mark.parametrize("entity_type", ["album", "artist", "track", "playlist"])
    def test_every_entity_type_is_linked_alike(self, module, entity_type):
        _write_art(module, entity_type, f"{entity_type}_1")
        urls = module._resource_image_urls(entity_type, f"{entity_type}_1.jpg")

        assert _path(urls.small) == f"/resource/{entity_type}/{entity_type}_1_small.jpg"


class TestWhenThereIsNoImage:
    def test_no_image_url_gives_nothing(self, module):
        assert module._resource_image_urls("album", None) is None
        assert module._resource_image_urls("album", "") is None

    def test_a_recorded_image_with_no_file_gives_nothing(self, module):
        """The row can name a cover the artwork folder never received."""
        assert module._resource_image_urls("album", "album_missing.jpg") is None
