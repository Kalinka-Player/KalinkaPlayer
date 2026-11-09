"""The loose-track group is found by the name the app shows it under, and its
tracks carry that name, whatever title its row stores."""

import asyncio
import sqlite3

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterQuery
from kalinka_plugin_sdk.inputmodule import SearchType
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import (
    SINGLES_TITLE,
    ListingFilter,
    LocalFilesInputModuleDb,
)
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

SHOWN = ["Singles & loose tracks", "singles loose", "LOOSE"]
STORED = ["Unknown Album", "unknown album"]


def _write_flac(path, tags):
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for key, value in tags.items():
        audio[key] = value
    audio.save()


@pytest.fixture
def library(tmp_path):
    """One track without an album tag, and one on a named album."""
    music = tmp_path / "music"
    (music / "Moon Safari").mkdir(parents=True)
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    paths = (music / "bee.flac", music / "Moon Safari" / "sexy.flac")
    _write_flac(paths[0], {"title": "Bee Moved", "artist": "Blue Coast"})
    _write_flac(
        paths[1], {"title": "Sexy Boy", "artist": "Air", "album": "Moon Safari"}
    )

    async def index():
        await init_db(config.db_path)
        indexer = FileIndexer(config, AsyncIndexerDb(config))
        return [(await indexer.process_file(str(path)))["tracks"] for path in paths]

    loose, named = asyncio.run(index())
    db = LocalFilesInputModuleDb(config)
    assert db.get_track_by_id(loose)["album_id"] == "unknown_album"
    assert db.get_track_by_id(named)["album_id"] != "unknown_album"
    assert db.get_album_by_id("unknown_album")["title"] == "Unknown Album"
    return LocalFilesInputModule(config, db), str(music), loose, named


def _browse(module, endpoint, text):
    return asyncio.run(
        module.browse(
            EntityId(id=endpoint, type=EntityType.CATALOG, source="localfiles"),
            filter=FilterQuery.model_validate({"q": {"contains": text}}),
        )
    )


def _ids(listing):
    return [item.id.id for item in listing.items]


@pytest.mark.parametrize("query", SHOWN)
def test_album_search_finds_the_group_by_its_shown_name(library, query):
    module, *_ = library
    albums, total = module.db_manager.search_albums(query)
    assert [album["id"] for album in albums] == ["unknown_album"]
    assert total == 1

    found = asyncio.run(module.search(SearchType.album, query))
    assert [item.name for item in found.items] == [SINGLES_TITLE]
    assert found.items[0].id.type == EntityType.PLAYLIST


@pytest.mark.parametrize("query", STORED)
def test_album_search_no_longer_finds_the_group_by_its_stored_title(library, query):
    module, *_ = library
    assert module.db_manager.search_albums(query) == ([], 0)
    assert asyncio.run(module.search(SearchType.album, query)).items == []


@pytest.mark.parametrize("query", ["unknown", "artist"])
def test_album_search_does_not_find_the_group_by_its_placeholder_artist(
    library, query
):
    module, *_ = library
    assert module.db_manager.search_albums(query) == ([], 0)


def test_album_search_orders_by_the_shown_title_whatever_its_case(library):
    module, _, _, named = library
    album_id = module.db_manager.get_track_by_id(named)["album_id"]
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute(
            "UPDATE albums SET title = 'moon safari' WHERE id = ?", (album_id,)
        )
    albums, _ = module.db_manager.search_albums("s")
    assert [album["id"] for album in albums] == [album_id, "unknown_album"]


def test_album_search_leaves_out_the_group_while_it_is_empty(library):
    module, _, loose, _ = library
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute("DELETE FROM tracks WHERE id = ?", (loose,))
    assert module.db_manager.search_albums("singles loose") == ([], 0)


def test_a_named_album_is_still_found_by_its_title(library):
    module, *_ = library
    albums, total = module.db_manager.search_albums("moon safari")
    assert [album["title"] for album in albums] == ["Moon Safari"]
    assert total == 1
    assert _ids(_browse(module, "albums", "safari air")) == [albums[0]["id"]]


@pytest.mark.parametrize("query", SHOWN)
def test_the_library_finds_the_group_and_its_track_by_its_shown_name(library, query):
    module, _, loose, _ = library
    rows, total = module.db_manager.get_library_listing(
        filter=ListingFilter(text=query)
    )
    assert sorted((kind, row["id"]) for kind, row in rows) == [
        ("album", "unknown_album"),
        ("track", loose),
    ]
    assert total == 2
    page, paged_total = module.db_manager.get_library_listing(
        offset=1, limit=1, filter=ListingFilter(text=query)
    )
    assert paged_total == total and len(page) == 1

    assert set(_ids(_browse(module, "library", query))) == {"unknown_album", loose}


@pytest.mark.parametrize("query", STORED)
def test_the_library_no_longer_finds_the_group_by_its_stored_title(library, query):
    module, *_ = library
    assert module.db_manager.get_library_listing(
        filter=ListingFilter(text=query)
    ) == ([], 0)
    assert module.db_manager.search_tracks(query) == ([], 0)


@pytest.mark.parametrize(
    "query,found", [(q, True) for q in SHOWN] + [(q, False) for q in STORED]
)
def test_folder_search_matches_a_loose_track_by_its_shown_album(
    library, query, found
):
    module, music, loose, _ = library
    match = ListingFilter(text=query)
    tracks, total = module.db_manager.list_folder_tracks(music, filter=match)
    assert [track["id"] for track in tracks] == ([loose] if found else [])
    assert total == (1 if found else 0)
    assert module.db_manager.holds_tracks(music, match) is found
    assert _ids(_browse(module, "files", query)) == ([loose] if found else [])


def test_a_loose_track_names_its_group_wherever_it_is_listed(library):
    from kalinka_server.queue_add import tracks_for

    module, _, loose, named = library
    group = EntityId(id="unknown_album", type=EntityType.PLAYLIST, source="localfiles")
    listed = asyncio.run(module.browse(group))
    assert [item.track.album.title for item in listed.items] == [SINGLES_TITLE]

    found = asyncio.run(module.search(SearchType.track, "bee moved"))
    assert [item.track.album.title for item in found.items] == [SINGLES_TITLE]

    queued = asyncio.run(
        tracks_for([group.to_string], lambda eid: module, lambda source: module)
    )
    assert [(track.id.id, track.album.title) for track in queued] == [
        (loose, SINGLES_TITLE)
    ]

    infos = asyncio.run(module.get_track_info([loose, named]))
    assert [info.metadata.album.title for info in infos] == [
        SINGLES_TITLE,
        "Moon Safari",
    ]
    assert infos[0].metadata.album.id.id == "unknown_album"
