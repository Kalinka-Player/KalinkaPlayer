"""Decades: read out of a search query, and offered as a library filter by the
year an album first came out."""

import asyncio
import sqlite3

import pytest

from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterQuery, UnsupportedFilter

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import ListingFilter, LocalFilesInputModuleDb
from kalinka_plugin_localfiles.searcher.query_terms import decades_in, year_spans


@pytest.mark.parametrize(
    "query,decades",
    [
        ("70s prog rock", [1970]),
        ("music from the 1980s", [1980]),
        ("'60s and 70's soul", [1960, 1970]),
        ("the eighties synth pop", [1980]),
        ("00s indie", [2000]),
        ("20s jazz", [1920]),
        ("2010s pop", [2010]),
        ("sad piano", []),
        ("songs from 1975", []),
        ("top 40 hits", []),
        ("90s and 90s", [1990]),
    ],
)
def test_a_query_names_its_decades(query, decades):
    assert decades_in(query) == decades


def test_a_decade_spans_ten_years():
    assert year_spans([1970, 2000]) == [(1970, 1979), (2000, 2009)]


def _seed(path):
    albums = [
        # id, title, year, original_year
        ("al-dsotm", "Dark Side", 1973, None),
        ("al-bell", "Division Bell (remaster)", 2011, 1994),
        ("al-avalon", "Avalon", 1982, 1982),
        ("al-undated", "Undated", 0, None),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO artists (id, name, last_updated) VALUES ('ar-pf', 'Pink Floyd', 1)")
        conn.execute("INSERT INTO artists (id, name, last_updated) VALUES ('ar-rm', 'Roxy Music', 1)")
        for id, title, year, original in albums:
            artist = "ar-rm" if id == "al-avalon" else "ar-pf"
            conn.execute(
                "INSERT INTO albums (id, title, artist_id, year, original_year, last_updated)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (id, title, artist, year, original),
            )
            conn.execute(
                "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format, last_updated)"
                " VALUES (?, ?, ?, ?, ?, 'flac', 1)",
                (f"t-{id}", f"{title} song", id, artist, f"/music/{id}.flac"),
            )


@pytest.fixture
def db(tmp_path):
    cfg = LocalFilesConfig(db_path=str(tmp_path / "d.db"), artwork_path=str(tmp_path / "art"))
    asyncio.run(init_db(cfg.db_path))
    _seed(cfg.db_path)
    return LocalFilesInputModuleDb(cfg)


def _titles(rows):
    return sorted(row["title"] for row in rows)


def test_albums_are_filtered_by_the_decade_they_first_came_out(db):
    nineties, total = db.list_kind("album", 0, 50, ListingFilter(decades=(1990,)))

    assert _titles(nineties) == ["Division Bell (remaster)"]
    assert total == 1


def test_any_of_several_decades_matches(db):
    rows, _ = db.list_kind("album", 0, 50, ListingFilter(decades=(1970, 1980)))

    assert _titles(rows) == ["Avalon", "Dark Side"]


def test_tracks_and_artists_follow_their_albums(db):
    tracks, _ = db.list_kind("track", 0, 50, ListingFilter(decades=(1980,)))
    artists, _ = db.list_kind("artist", 0, 50, ListingFilter(decades=(1970,)))

    assert _titles(tracks) == ["Avalon song"]
    assert [row["name"] for row in artists] == ["Pink Floyd"]


def test_the_vocabulary_lists_dated_decades_oldest_first_with_album_counts(db):
    assert db.list_album_decades() == [(1970, 1), (1980, 1), (1990, 1)]


@pytest.fixture
def module(db):
    from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

    module = LocalFilesInputModule.__new__(LocalFilesInputModule)
    module.db_manager = db
    module._music_folders = []
    module._searcher = None
    return module


def _catalog(endpoint):
    return EntityId(id=endpoint, type=EntityType.CATALOG, source="localfiles")


def test_album_shelf_declares_and_honours_the_decade(module):
    result = asyncio.run(
        module.browse(_catalog("albums"), filter=FilterQuery.model_validate({"decade": {"any": ["1990"]}}))
    )
    values = asyncio.run(module.list_filter_values(_catalog("albums"), "decade"))

    assert [item.name for item in result.items] == ["Division Bell (remaster)"]
    assert [(v.id, v.name, v.count) for v in values.items] == [
        ("1970", "1970s", 1),
        ("1980", "1980s", 1),
        ("1990", "1990s", 1),
    ]


@pytest.mark.parametrize(
    "selector",
    [{"any": ["1975"]}, {"any": ["seventies"]}, {"none": ["1970"]}],
)
def test_a_value_that_is_not_a_decade_is_refused(module, selector):
    with pytest.raises(UnsupportedFilter):
        asyncio.run(
            module.browse(_catalog("albums"), filter=FilterQuery.model_validate({"decade": selector}))
        )


def test_playlists_do_not_offer_a_decade(module):
    with pytest.raises(UnsupportedFilter):
        asyncio.run(module.list_filter_values(_catalog("playlists"), "decade"))
