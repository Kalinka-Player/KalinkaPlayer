"""Direct library navigation, the loose-track umbrella, and bundled artwork."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from kalinka_plugin_sdk.datamodel import (
    EntityId,
    EntityType,
    PreviewContentType,
    PreviewType,
)
from kalinka_plugin_sdk.filters import FilterQuery
from kalinka_plugin_localfiles.catalog_art import ART_DIRECTORY, ART_CATEGORIES
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.enricher.enricher_db import AsyncEnricherDb
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule


@pytest.fixture
def module(tmp_path):
    config = LocalFilesConfig(
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "covers"),
    )
    asyncio.run(init_db(config.db_path))
    with sqlite3.connect(config.db_path) as conn:
        conn.executemany(
            "INSERT INTO artists (id, name, last_updated) VALUES (?, ?, ?)",
            [("z", "Zulu", 300), ("a", "alpha", 100)],
        )
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id, genre, last_updated)"
            " VALUES (?, ?, 'z', 'Jazz', ?)",
            [
                ("zebra", "Zebra", 300),
                ("echoes", "Échoes", 100),
                ("single", "A named single", 200),
            ],
        )
        conn.executemany(
            "INSERT INTO tracks (id,title,album_id,artist_id,file_path,format,duration,"
            "last_updated) VALUES (?,?,?,?,?,'flac',60,?)",
            [
                ("t_z", "Zebra song", "zebra", "z", "/music/z.flac", 600),
                ("t_e", "écho", "echoes", "z", "/music/e.flac", 500),
                ("t_s", "Alone", "single", "a", "/music/s.flac", 400),
                (
                    "loose_known",
                    "Loose song",
                    "unknown_album",
                    "a",
                    "/music/l.flac",
                    200,
                ),
                (
                    "loose_unknown",
                    "another song",
                    "unknown_album",
                    "unknown_artist",
                    "/music/u.flac",
                    100,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO library_file (file_id, current_path, first_indexed)"
            " SELECT id, file_path, last_updated FROM tracks"
        )
        conn.execute(
            "INSERT INTO playlists (id,name,created_by,created_at,last_updated)"
            " VALUES ('mix','Evening','test',100,100)"
        )
    return LocalFilesInputModule(config, LocalFilesInputModuleDb(config))


def browse(module, endpoint, offset=0, limit=50, filter=None, type=EntityType.CATALOG):
    return asyncio.run(
        module.browse(
            EntityId(id=endpoint, type=type, source="localfiles"),
            offset=offset,
            limit=limit,
            filter=FilterQuery.model_validate(filter or {}),
        )
    )


def ids(listing):
    return [item.id.id for item in listing.items]


def test_root_categories_have_stable_ids_and_page_without_duplicates(module):
    root = browse(module, "root")
    assert ids(root) == ["files", "albums", "artists", "tracks", "recent", "playlists"]
    assert [item.name for item in root.items] == [
        "Folders",
        "Albums",
        "Artists",
        "Tracks",
        "Recently added",
        "Playlists",
    ]
    assert all(item.can_browse and item.catalog for item in root.items)
    assert [
        id
        for offset in range(0, root.total, 2)
        for id in ids(browse(module, "root", offset, 2))
    ] == ids(root)
    assert browse(module, "root", limit=0).total == root.total
    assert not browse(module, "root", offset=20).items


def test_empty_library_keeps_folders_without_phantom_categories(module):
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute("DELETE FROM tracks")
        conn.execute("DELETE FROM playlists")
    assert ids(browse(module, "root")) == ["files"]


def test_loose_umbrella_is_first_and_named_single_remains_an_album(module):
    albums = browse(module, "albums")
    assert ids(albums) == ["unknown_album", "single", "echoes", "zebra"]
    group = albums.items[0]
    assert group.name == group.playlist.name == "Singles & loose tracks"
    assert group.id.type == EntityType.PLAYLIST
    assert group.album is None and group.catalog is None
    assert group.playlist.id == group.id
    assert group.playlist.track_count == 2
    assert group.playlist.duration == 120
    assert group.subname == group.playlist.description == "2 loose tracks"
    assert group.can_add and group.can_browse
    assert group.can_edit is False
    assert group.playlist.image.aspect_ratio == 1
    assert group.sections[0].id == group.id
    assert group.sections[0].catalog.preview_config.type == PreviewType.TILE
    assert (
        group.sections[0].catalog.preview_config.content_type
        == PreviewContentType.TRACK
    )
    assert albums.items[1].id.type == EntityType.ALBUM
    assert albums.items[1].album.title == "A named single"
    assert [
        id
        for offset in range(albums.total)
        for id in ids(browse(module, "albums", offset, 1))
    ] == ids(albums)
    assert browse(module, "albums", limit=0).total == 4
    assert not browse(module, "albums", limit=0).items
    assert not browse(module, "albums", offset=4).items
    with sqlite3.connect(module.db_manager.db_path) as conn:
        assert (
            conn.execute(
                "SELECT title FROM albums WHERE id='unknown_album'"
            ).fetchone()[0]
            == "Unknown Album"
        )


@pytest.mark.parametrize("container_type", [EntityType.PLAYLIST, EntityType.ALBUM])
def test_umbrella_browses_all_loose_tracks_and_keeps_old_links(module, container_type):
    tracks = browse(module, "unknown_album", type=container_type)
    assert ids(tracks) == ["loose_unknown", "loose_known"]
    assert tracks.total == 2
    assert ids(browse(module, "unknown_album", 1, 1, type=container_type)) == [
        "loose_known"
    ]
    assert browse(module, "unknown_album", limit=0, type=container_type).total == 2
    assert not browse(module, "unknown_album", offset=2, type=container_type).items
    detail = asyncio.run(
        module.get(
            EntityId(id="unknown_album", type=container_type, source="localfiles")
        )
    )
    assert detail.name == "Singles & loose tracks"
    assert detail.id.type == EntityType.PLAYLIST
    assert detail.album is None
    assert detail.playlist.track_count == tracks.total


@pytest.mark.parametrize("count", [0, 1, 2])
def test_loose_track_subtitle_uses_current_membership_instead_of_cached_stats(
    module, count
):
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute("UPDATE albums SET track_count=99 WHERE id='unknown_album'")
        if count < 2:
            conn.execute("DELETE FROM tracks WHERE id='loose_known'")
        if count == 0:
            conn.execute("DELETE FROM tracks WHERE id='loose_unknown'")
    detail = asyncio.run(
        module.get(
            EntityId(id="unknown_album", type=EntityType.PLAYLIST, source="localfiles")
        )
    )
    subtitle = f"{count} loose {'track' if count == 1 else 'tracks'}"
    assert detail.subname == detail.playlist.description == subtitle
    assert detail.playlist.track_count == count
    assert detail.playlist.duration == count * 60
    assert detail.can_add == (count > 0)
    assert "unknown_album" not in ids(browse(module, "playlists"))


@pytest.mark.parametrize("container_type", [EntityType.PLAYLIST, EntityType.ALBUM])
def test_loose_group_still_expands_to_playable_tracks(module, container_type):
    from kalinka_server.queue_add import tracks_for

    entity_id = EntityId(id="unknown_album", type=container_type, source="localfiles")
    tracks = asyncio.run(
        tracks_for([entity_id.to_string], lambda eid: module, lambda source: module)
    )
    assert [track.id.id for track in tracks] == ["loose_unknown", "loose_known"]


@pytest.mark.parametrize(
    "operation,args",
    [
        ("playlist_update", ("Changed", "Changed")),
        ("playlist_delete", ()),
        ("playlist_add_tracks", (["kalinka:localfiles:track:t_s"],)),
        ("playlist_remove_tracks", (["loose_known"],)),
    ],
)
def test_loose_group_is_read_only(module, operation, args):
    entity_id = EntityId(
        id="unknown_album", type=EntityType.PLAYLIST, source="localfiles"
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(module, operation)(entity_id.to_string, *args))
    assert exc.value.status_code == 405
    assert module.db_manager.get_album_track_totals("unknown_album") == (2, 120)
    assert module.db_manager.get_playlist_by_id("unknown_album") is None


def test_empty_umbrella_is_omitted(module):
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute("DELETE FROM tracks WHERE album_id='unknown_album'")
    albums = browse(module, "albums")
    assert ids(albums) == ["single", "echoes", "zebra"]
    assert albums.total == 3


def test_unknown_artist_does_not_claim_other_artists_loose_tracks(module):
    page = browse(module, "unknown_artist", type=EntityType.ARTIST)
    assert ids(page) == ["loose_unknown"]
    artist = module.db_manager.get_artist_by_id("unknown_artist")
    assert artist["album_count"] == 0


@pytest.mark.parametrize("writer", [AsyncIndexerDb, AsyncEnricherDb])
def test_indexing_and_enrichment_count_all_umbrella_tracks(module, writer):
    asyncio.run(writer(module.config).update_album_stats("unknown_album"))
    album = module.db_manager.get_album_by_id("unknown_album")
    assert album["track_count"] == 2
    assert album["duration"] == 120


def test_album_filters_match_visible_group_name_and_correct_totals(module):
    group = browse(module, "albums", filter={"q": {"contains": "singles loose"}})
    assert ids(group) == ["unknown_album"]
    assert group.total == 1
    filtered = browse(module, "albums", filter={"genre": {"any": ["jazz"]}})
    assert ids(filtered) == ["single", "echoes", "zebra"]
    assert filtered.total == 3
    missing = browse(module, "albums", filter={"q": {"contains": "not present"}})
    assert missing.total == 0 and missing.items == []


def test_artists_and_tracks_are_alphabetical_while_recent_stays_by_arrival(module):
    assert ids(browse(module, "artists")) == ["a", "unknown_artist", "z"]
    assert ids(browse(module, "tracks")) == [
        "t_s",
        "loose_unknown",
        "t_e",
        "loose_known",
        "t_z",
    ]
    assert ids(browse(module, "recent-tracks")) == [
        "t_z",
        "t_e",
        "t_s",
        "loose_known",
        "loose_unknown",
    ]


def test_each_root_artwork_is_packaged_and_served_without_library_covers(module):
    root = browse(module, "root")
    assert {item.id.id for item in root.items} == set(ART_CATEGORIES)
    large_images = set()
    for item in root.items:
        art = item.catalog.image
        assert art and art.aspect_ratio == 3
        large_images.add(art.large)
        for size, dimensions in (
            ("large", (1200, 400)),
            ("small", (600, 200)),
            ("thumbnail", (400, 400)),
        ):
            resource = getattr(art, size).removeprefix("/resource/")
            path = asyncio.run(module.get_resource_path(resource))
            assert path and Path(path).parent == ART_DIRECTORY
            with Image.open(path) as image:
                assert image.size == dimensions
                image.verify()
    assert len(large_images) == len(root.items)


def test_artist_artwork_refreshes_while_old_links_still_resolve(module):
    artist = next(
        item for item in browse(module, "root").items if item.id.id == "artists"
    )
    assert "artists-v2-large.jpg" in artist.catalog.image.large
    assert asyncio.run(module.get_resource_path("catalog-art/artists-v1-large.jpg"))


@pytest.mark.parametrize(
    "resource",
    [
        "catalog-art/../config_model.py",
        "catalog-art/../../README.md",
        "catalog-art/prompts.json",
        "catalog-art/files-v1-large.jpg/extra",
        "catalog-art/%2e%2e/config_model.py",
        "catalog-art/unknown-v1-large.jpg",
        "catalog-art//etc/passwd",
    ],
)
def test_packaged_art_route_does_not_expose_other_files(module, resource):
    assert asyncio.run(module.get_resource_path(resource)) is None
