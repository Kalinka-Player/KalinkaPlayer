"""A folder's cover is made from the covers under it, composed when first
asked for; a playlist's from its albums' covers by the same renderer."""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3

import pytest
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.folder_ids import encode_folder_id
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.utils.artwork_store import store_decoded_image
from kalinka_plugin_localfiles.utils.cover_collage import render_mosaic
from kalinka_plugin_localfiles.utils.folder_collage import FolderCovers

RED, GREEN, BLUE, YELLOW, WHITE = (
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 0),
    (255, 255, 255),
)

ALBUMS = {
    "album_000000000000000a": RED,
    "album_000000000000000b": GREEN,
    "album_000000000000000c": BLUE,
    "album_000000000000000d": YELLOW,
    "album_000000000000000e": WHITE,
}
SINGLE = "track_00000000000000f1"


def _cover(artwork, kind, image_id, colour):
    store_decoded_image(Image.new("RGB", (300, 300), colour), artwork, image_id, kind)


def _seed(db_path: str) -> None:
    albums = list(ALBUMS)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO artists (id, name) VALUES ('ar', 'Artist')")
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id, image_url) VALUES (?, ?, 'ar', ?)",
            [(album, album, f"{album}.jpg") for album in albums]
            + [("album_bare", "bare", "")],
        )
        rows = [
            (f"t{index}", album, f"/music/many/{index}/x.flac", None)
            for index, album in enumerate(albums)
        ]
        rows += [
            ("t_a2", albums[0], "/music/many/0/y.flac", None),
            ("t_single", "unknown_album", "/music/one/a.flac", f"{SINGLE}.jpg"),
            ("t_bare", "album_bare", "/music/one/b.flac", None),
            ("t_pair1", albums[1], "/music/pair/a.flac", None),
            ("t_pair2", albums[2], "/music/pair/b.flac", None),
        ]
        conn.executemany(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format,"
            " duration, image_url) VALUES (?, ?, ?, 'ar', ?, 'flac', 60, ?)",
            [(id, id, album, path, image) for id, album, path, image in rows],
        )
        conn.execute(
            "INSERT INTO playlists (id, name, description, created_by, created_at,"
            " last_updated) VALUES ('pl', 'P', '', 'test', 0, 0)"
        )
        conn.executemany(
            "INSERT INTO playlist_tracks (playlist_track_id, playlist_id, track_id,"
            " position, added_at) VALUES (?, 'pl', ?, ?, 0)",
            [(f"pt{index}", f"t{index}", index) for index in range(len(albums))],
        )


class _Roots:
    def canonical_roots(self, locations):
        return ["/music"]


@pytest.fixture
def artwork(tmp_path):
    path = tmp_path / "artwork"
    for album, colour in ALBUMS.items():
        _cover(path, "album", album, colour)
    _cover(path, "track", SINGLE, WHITE)
    return path


@pytest.fixture
def config(tmp_path, artwork):
    cfg = LocalFilesConfig(db_path=str(tmp_path / "test.db"), artwork_path=str(artwork))
    asyncio.run(init_db(cfg.db_path))
    _seed(cfg.db_path)
    return cfg


@pytest.fixture
def db(config):
    return LocalFilesInputModuleDb(config)


@pytest.fixture
def module(config, db):
    return LocalFilesInputModule(config, db, storage_source=_Roots)


def _quadrants(image):
    side = image.width
    return [
        image.getpixel((x, y))
        for y in (side // 4, 3 * side // 4)
        for x in (side // 4, 3 * side // 4)
    ]


def _close(pixel, colour):
    return all(abs(a - b) < 16 for a, b in zip(pixel, colour))


def test_covers_are_the_distinct_ones_in_path_order(db):
    assert db.get_folder_covers("/music/many", 3) == [
        ("album", "album_000000000000000a"),
        ("album", "album_000000000000000b"),
        ("album", "album_000000000000000c"),
    ]


def test_a_single_without_an_album_cover_lends_its_own(db):
    assert db.get_folder_covers("/music/one", 8) == [("track", SINGLE)]


def test_each_subfolder_gets_its_own_covers_in_one_query(db):
    covers = db.get_subfolder_covers("/music", ["one", "pair", "absent"], 8)

    assert covers == {
        "one": [("track", SINGLE)],
        "pair": [
            ("album", "album_000000000000000b"),
            ("album", "album_000000000000000c"),
        ],
    }


FIRST_FOUR = [("album", album) for album in list(ALBUMS)[:4]]
KEY = "a000000000000000a-a000000000000000b-a000000000000000c-a000000000000000d"


def _covers(artwork, listed=None):
    """Folder covers whose library is ``listed``: folder to its covers."""
    listed = {"/music/many": FIRST_FOUR} if listed is None else listed
    return FolderCovers(artwork, listed.get)


def _resource(folder, key=KEY, size="large"):
    return f"folder/{encode_folder_id(folder)}/{key}_{size}.jpg"


def _mosaics(artwork):
    return sorted(path.name for path in (artwork / "folder").rglob("*.jpg"))


def test_no_covers_leave_the_folder_to_the_client(artwork):
    assert _covers(artwork).cover_of("/music/many", []) is None
    assert (
        _covers(artwork).cover_of("/music/many", [("album", "album_ffffffffffffffff")])
        is None
    )


def test_fewer_than_four_covers_show_the_first_alone(artwork):
    image = _covers(artwork).cover_of(
        "/music/pair",
        [("album", "album_000000000000000b"), ("album", "album_000000000000000c")],
    )

    assert image.large == "/resource/album/album_000000000000000b_large.jpg"


def test_a_cover_whose_file_is_missing_is_passed_over(artwork):
    image = _covers(artwork).cover_of(
        "/music/one", [("album", "album_ffffffffffffffff"), ("track", SINGLE)]
    )

    assert image.small == f"/resource/track/{SINGLE}_small.jpg"


def test_four_covers_are_named_for_the_folder_and_its_mosaic(artwork):
    image = _covers(artwork).cover_of("/music/many", FIRST_FOUR)

    assert image.thumbnail == "/resource/" + _resource("/music/many", size="thumbnail")


def test_the_mosaic_is_composed_when_first_asked_for(artwork):
    path = asyncio.run(_covers(artwork).path_of(_resource("/music/many")))

    with Image.open(path) as mosaic:
        assert mosaic.size == (600, 600)
        assert all(
            _close(pixel, colour)
            for pixel, colour in zip(_quadrants(mosaic), [RED, GREEN, BLUE, YELLOW])
        )
    assert _mosaics(artwork) == [
        f"{KEY}_large.jpg",
        f"{KEY}_small.jpg",
        f"{KEY}_thumbnail.jpg",
    ]
    assert not list((artwork / "folder").rglob("*.tmp"))


def test_a_composed_mosaic_is_not_composed_again(artwork):
    store = _covers(artwork)
    path = asyncio.run(store.path_of(_resource("/music/many", size="small")))
    before = pathlib.Path(path).stat().st_mtime_ns

    assert asyncio.run(store.path_of(_resource("/music/many", size="small"))) == path
    assert pathlib.Path(path).stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "resource",
    [
        # The folder's covers, but not in the order it leads with them.
        _resource(
            "/music/many",
            "a000000000000000b-a000000000000000a-a000000000000000c"
            "-a000000000000000d",
        ),
        # Covers that exist, but are not the folder's first four.
        _resource(
            "/music/many",
            "a000000000000000a-a000000000000000b-a000000000000000c"
            "-a000000000000000e",
        ),
        # The same covers again, for a folder not in the library.
        _resource("/music/elsewhere"),
        _resource("/music/many", size="huge"),
        f"folder/{KEY}_large.jpg",
        f"folder/not*an*id/{KEY}_large.jpg",
        "folder/../album/album_000000000000000a_large.jpg",
    ],
)
def test_a_name_that_is_not_the_folders_own_is_neither_served_nor_made(
    artwork, resource
):
    assert asyncio.run(_covers(artwork).path_of(resource)) is None
    assert _mosaics(artwork) == []


def test_a_mosaic_whose_covers_are_gone_is_not_made(artwork):
    (artwork / "album" / "album_000000000000000d_large.jpg").unlink()

    assert asyncio.run(_covers(artwork).path_of(_resource("/music/many"))) is None
    assert _mosaics(artwork) == []


def test_a_folder_keeps_one_mosaic_its_old_one_replaced(artwork):
    listed = {"/music/many": FIRST_FOUR}
    store = _covers(artwork, listed)
    asyncio.run(store.path_of(_resource("/music/many")))

    listed["/music/many"] = [("album", album) for album in list(ALBUMS)[1:]]
    newer = "a000000000000000b-a000000000000000c-a000000000000000d-a000000000000000e"
    assert asyncio.run(store.path_of(_resource("/music/many", newer))) is not None

    assert _mosaics(artwork) == [
        f"{newer}_large.jpg",
        f"{newer}_small.jpg",
        f"{newer}_thumbnail.jpg",
    ]


def test_a_listed_folder_points_at_its_mosaic_and_the_module_serves_it(module):
    from kalinka_plugin_sdk.datamodel import EntityId, EntityType

    library = asyncio.run(
        module.browse(
            EntityId(id="files", type=EntityType.CATALOG, source="localfiles")
        )
    )
    many = next(item for item in library.items if item.name == "many")
    resource = many.catalog.image.large.removeprefix("/resource/")

    assert resource.startswith("folder/")
    assert asyncio.run(module.get_resource_path(resource)) is not None


def test_a_resource_outside_the_artwork_folder_is_not_served(module, tmp_path):
    (tmp_path / "secret.txt").write_text("x")

    assert asyncio.run(module.get_resource_path("../secret.txt")) is None


def test_render_mosaic_crops_each_cover_square_in_reading_order():
    covers = [
        Image.new("RGB", (400, 200), colour) for colour in (RED, GREEN, BLUE, YELLOW)
    ]

    mosaic = render_mosaic(covers, side=200)

    assert mosaic.size == (200, 200)
    assert _quadrants(mosaic) == [RED, GREEN, BLUE, YELLOW]


def test_a_playlist_cover_is_a_mosaic_of_its_first_albums(module, artwork):
    assert module._generate_playlist_cover("pl") == "pl"

    with Image.open(artwork / "playlist" / "pl_large.jpg") as cover:
        assert cover.size == (600, 600)
        assert _close(_quadrants(cover)[3], YELLOW)
    for size in ("thumbnail", "small"):
        assert (artwork / "playlist" / f"pl_{size}.jpg").exists()
