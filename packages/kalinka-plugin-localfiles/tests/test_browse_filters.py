"""Filtering a shelf: the library's own genre vocabulary, and text over it."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from kalinka_plugin_sdk.datamodel import EntityId, EntityType, PreviewType
from kalinka_plugin_sdk.filters import FilterQuery, UnsupportedFilter

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import (
    ListingFilter,
    LocalFilesInputModuleDb,
    split_genres,
)


def _seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO artists (id, name, last_updated) VALUES (?, ?, ?)",
            [
                ("ar_queen", "Queen", 140),
                ("ar_air", "Air", 130),
                ("ar_bg", "Борис Гребенщиков", 120),
                ("ar_quiet", "Silent Partner", 110),
            ],
        )
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id, genre, last_updated)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                ("al_innuendo", "Innuendo", "ar_queen", "Rock", 106),
                ("al_hertz", "Everybody Hertz", "ar_air", "Electronic, Ambient", 105),
                ("al_moon", "Moon Safari", "ar_air", "electronic", 104),
                ("al_rock", "Русский альбом", "ar_bg", "Рок", 103),
                ("al_split", "Split Tags", "ar_quiet", "future pop/ebm", 102),
                ("al_none", "Untagged", "ar_quiet", None, 101),
            ],
        )
        conn.execute(
            "INSERT INTO playlists (id, name, description, created_by, created_at,"
            " last_updated) VALUES (?, ?, ?, ?, ?, ?)",
            ("pl_evening", "Evening", "quiet electronic sets", "test", 0, 100),
        )
        conn.executemany(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format,"
            " duration, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("t_show", "The Show Must Go On", "al_innuendo", "ar_queen", "/1",
                 "flac", 262, 93),
                ("t_playground", "Playground Love", "al_hertz", "ar_air", "/2",
                 "flac", 202, 92),
                ("t_sexy", "Sexy Boy", "al_moon", "ar_air", "/3", "flac", 298, 91),
            ],
        )
        # The schema's own "unknown" sentinels are stamped with the current
        # time, which would sit ahead of everything seeded here.
        conn.execute(
            "UPDATE artists SET last_updated = 10 WHERE id = 'unknown_artist'"
        )
        conn.execute(
            "UPDATE albums SET last_updated = 10 WHERE id = 'unknown_album'"
        )
        conn.commit()


@pytest.fixture
def db(tmp_path):
    cfg = LocalFilesConfig(
        db_path=str(tmp_path / "test.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    asyncio.run(init_db(cfg.db_path))
    _seed(cfg.db_path)
    return LocalFilesInputModuleDb(cfg)



@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Electronic, Ambient", ["electronic", "ambient"]),
        ("future pop/ebm", ["future pop", "ebm"]),
        ("Rock;Pop|Jazz", ["rock", "pop", "jazz"]),
        ("Рок", ["рок"]),
        ("  ", []),
        (None, []),
    ],
)
def test_multi_valued_tags_split_into_separate_genres(raw, expected):
    assert split_genres(raw) == expected


def test_vocabulary_counts_albums_and_keeps_the_library_spelling(db):
    values, total = db.list_album_genres(0, 50)
    by_id = {id: (name, count) for id, name, count in values}

    assert total == len(values)
    # "Electronic, Ambient" and "electronic" are the same genre, counted twice.
    assert by_id["electronic"] == ("Electronic", 2)
    assert by_id["ambient"] == ("Ambient", 1)
    assert by_id["рок"] == ("Рок", 1)
    # A split tag yields both halves as first-class values.
    assert by_id["future pop"][1] == 1 and by_id["ebm"][1] == 1


def test_vocabulary_is_ordered_by_album_count(db):
    values, _ = db.list_album_genres(0, 50)
    counts = [count for _, _, count in values]
    assert counts == sorted(counts, reverse=True)


def test_vocabulary_paginates_over_a_stable_order(db):
    everything, total = db.list_album_genres(0, 50)
    page, page_total = db.list_album_genres(1, 2)
    assert page_total == total
    assert page == everything[1:3]


def test_vocabulary_narrows_by_type_ahead(db):
    values, total = db.list_album_genres(0, 50, "elect")
    assert [id for id, _, _ in values] == ["electronic"]
    assert total == 1



def test_genre_matches_a_whole_value_not_a_substring(db):
    _, total = db.list_kind("album", 0, 50, ListingFilter(genre_any=("pop",)))
    # "future pop" is its own genre; selecting "pop" must not reach it.
    assert total == 0


def test_any_unions_and_all_intersects(db):
    _, any_total = db.list_kind("album", 
        0, 50, ListingFilter(genre_any=("rock", "ambient"))
    )
    assert any_total == 2

    _, all_total = db.list_kind("album", 
        0, 50, ListingFilter(genre_all=("electronic", "ambient"))
    )
    assert all_total == 1


def test_none_excludes_and_keeps_untagged_albums(db):
    _, everything = db.list_kind("album", 0, 50)
    albums, total = db.list_kind("album", 0, 50, ListingFilter(genre_none=("electronic",)))
    titles = {album["title"] for album in albums}

    # Both electronic albums drop out; an album with no genre at all does not.
    assert total == everything - 2
    assert "Untagged" in titles
    assert {"Moon Safari", "Everybody Hertz"} & titles == set()


def test_artists_match_on_their_albums_genres(db):
    artists, total = db.list_kind("artist", 0, 50, ListingFilter(genre_any=("ambient",)))
    assert [artist["name"] for artist in artists] == ["Air"]
    assert total == 1


def test_tracks_inherit_their_albums_genre(db):
    tracks, total = db.list_kind("track", 
        0, 50, ListingFilter(genre_any=("electronic",))
    )
    assert {track["title"] for track in tracks} == {"Playground Love", "Sexy Boy"}
    assert total == 2



def test_album_text_spans_title_and_artist(db):
    _, by_title = db.list_kind("album", 0, 50, ListingFilter(text="safari"))
    _, by_artist = db.list_kind("album", 0, 50, ListingFilter(text="queen"))
    _, by_both = db.list_kind("album", 0, 50, ListingFilter(text="air moon"))
    assert (by_title, by_artist, by_both) == (1, 1, 1)


def test_track_text_spans_title_album_and_artist(db):
    _, total = db.list_kind("track", 0, 50, ListingFilter(text="hertz love"))
    assert total == 1


def test_text_folds_case_and_diacritics(db):
    _, total = db.list_kind("album", 0, 50, ListingFilter(text="ГРЕБЕНЩИКОВ"))
    assert total == 1


def test_text_and_genre_both_have_to_hold(db):
    _, total = db.list_kind("album", 
        0, 50, ListingFilter(text="air", genre_any=("ambient",))
    )
    assert total == 1

    _, none = db.list_kind("album", 
        0, 50, ListingFilter(text="queen", genre_any=("ambient",))
    )
    assert none == 0


def test_total_counts_the_filtered_listing_so_pages_stay_meaningful(db):
    page, total = db.list_kind("album", 0, 1, ListingFilter(genre_any=("electronic",)))
    assert len(page) == 1
    assert total == 2


def test_playlists_filter_on_name_and_description(db):
    _, by_name = db.list_kind("playlist", 0, 50, ListingFilter(text="evening"))
    _, by_description = db.list_kind("playlist", 0, 50, ListingFilter(text="quiet"))
    _, miss = db.list_kind("playlist", 0, 50, ListingFilter(text="morning"))
    assert (by_name, by_description, miss) == (1, 1, 0)



def _catalog(endpoint: str) -> EntityId:
    return EntityId(id=endpoint, type=EntityType.CATALOG, source="localfiles")


@pytest.fixture
def module(tmp_path, db):
    from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

    module = LocalFilesInputModule.__new__(LocalFilesInputModule)
    module.db_manager = db
    module._music_folders = []
    return module


def _root_card(module):
    from kalinka_plugin_localfiles import localfiles as lf

    root = lf.LocalFilesInputModule._browse_root(module, 0, 10)
    assert [item.id.id for item in root.items] == ["files", "recent"]
    return root.items[0]


def test_root_offers_the_library_as_one_catalog_of_its_folders(module):
    card = _root_card(module)

    assert card.id.id == "files"
    assert card.catalog.title == "My Library"
    assert card.catalog.preview_config.type is PreviewType.FOLDER
    assert card.catalog.filters == []


def test_the_library_carries_its_playlists(module):
    sections = _root_card(module).sections

    assert [section.id.id for section in sections] == ["playlists"]
    assert all(section.can_browse for section in sections)


def test_shelves_declare_what_they_can_filter(module):
    declared = {
        section.id.id: {spec.id for spec in section.catalog.filters}
        for section in _root_card(module).sections
    }
    # Declared so the shelf answers a genre query like the others — a playlist
    # carries none, so one it must satisfy leaves it empty.
    assert declared["playlists"] == {"q", "genre"}


def test_browse_honours_the_document(module):
    result = asyncio.run(
        module.browse(
            _catalog("albums"),
            filter=FilterQuery.model_validate({"genre": {"any": ["ambient"]}}),
        )
    )
    assert [item.name for item in result.items] == ["Everybody Hertz"]


@pytest.mark.parametrize(
    "shelf,field,selector",
    [
        ("playlists", "year", {"gte": 1990}),
        # `type` belongs to the library; a section is already one kind.
        ("albums", "type", {"any": ["album"]}),
    ],
)
def test_an_undeclared_field_is_refused_rather_than_ignored(
    module, shelf, field, selector
):
    with pytest.raises(UnsupportedFilter) as excinfo:
        asyncio.run(
            module.browse(
                _catalog(shelf),
                filter=FilterQuery.model_validate({field: selector}),
            )
        )
    assert excinfo.value.field == field


@pytest.mark.parametrize(
    "entity",
    [
        EntityId(id="al_moon", type=EntityType.ALBUM, source="localfiles"),
        EntityId(id="ar_air", type=EntityType.ARTIST, source="localfiles"),
        EntityId(id="pl_evening", type=EntityType.PLAYLIST, source="localfiles"),
        EntityId(id="root", type=EntityType.CATALOG, source="localfiles"),
    ],
)
def test_a_listing_that_offers_no_filters_refuses_one(module, entity):
    # These list an album's tracks, an artist's albums, a playlist, the shelf
    # index — none declares a field, so none may quietly answer unfiltered.
    with pytest.raises(UnsupportedFilter):
        asyncio.run(
            module.browse(
                entity, filter=FilterQuery.model_validate({"q": {"contains": "x"}})
            )
        )


def test_values_endpoint_serves_the_genre_vocabulary(module):
    values = asyncio.run(
        module.list_filter_values(_catalog("albums"), "genre", 0, 3)
    )
    assert [value.id for value in values.items][:1] == ["electronic"]
    assert values.items[0].count == 2

    with pytest.raises(UnsupportedFilter):
        asyncio.run(module.list_filter_values(_catalog("albums"), "year"))
    with pytest.raises(UnsupportedFilter):
        asyncio.run(module.list_filter_values(_catalog("albums"), "type"))


def _kind_of(item):
    for kind in ("track", "album", "artist", "playlist"):
        if getattr(item, kind) is not None:
            return kind
    return None


def _library(module, filter=None, offset=0, limit=50):
    return asyncio.run(
        module.browse(
            _catalog("library"),
            offset=offset,
            limit=limit,
            filter=filter or FilterQuery({}),
        )
    )


def test_the_library_lists_items_not_its_sections(module):
    """A consumer with no interest in the sections — a home shelf preview, the
    card art collage — browses this and must get things to show."""
    result = _library(module)

    assert result.items
    assert all(item.catalog is None for item in result.items)
    assert result.total == len(result.items)


def test_the_library_interleaves_every_kind(module):
    kinds = {
        _kind_of(item) for item in _library(module).items
    }
    assert kinds == {"track", "album", "artist", "playlist"}


def test_the_library_is_ordered_newest_first(module):
    rows, _ = module.db_manager.get_library_listing()
    timestamps = [row["last_updated"] for _, row in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_type_narrows_the_library_to_one_kind(module):
    result = _library(
        module, FilterQuery.model_validate({"type": {"any": ["album"]}})
    )

    assert result.items
    assert {_kind_of(item) for item in result.items} == {"album"}


def test_type_takes_several_kinds_at_once(module):
    result = _library(
        module, FilterQuery.model_validate({"type": {"any": ["album", "artist"]}})
    )

    assert {_kind_of(item) for item in result.items} == {"album", "artist"}


def test_a_section_and_the_library_under_its_type_agree(module):
    """The two addresses for one listing: the section exists so a consumer can
    preview a kind without knowing how to write the constraint."""
    section = asyncio.run(module.browse(_catalog("albums")))
    narrowed = _library(
        module, FilterQuery.model_validate({"type": {"any": ["album"]}})
    )

    assert narrowed.total == section.total
    assert [item.id.id for item in narrowed.items] == [
        item.id.id for item in section.items
    ]


def test_text_reaches_every_kind_at_once(module):
    result = _library(module, FilterQuery.model_validate({"q": {"contains": "air"}}))

    assert {_kind_of(item) for item in result.items} >= {"album", "artist"}


def test_a_genre_a_playlist_cannot_have_excludes_it(module):
    """A playlist carries no genre, so one it must satisfy leaves none — and
    one it must not is satisfied by every playlist."""
    required = _library(
        module, FilterQuery.model_validate({"genre": {"any": ["rock"]}})
    )
    excluded = _library(
        module, FilterQuery.model_validate({"genre": {"none": ["rock"]}})
    )

    assert "playlist" not in {_kind_of(item) for item in required.items}
    assert "playlist" in {_kind_of(item) for item in excluded.items}


def test_an_unknown_type_value_is_refused(module):
    with pytest.raises(UnsupportedFilter) as excinfo:
        _library(module, FilterQuery.model_validate({"type": {"any": ["genre"]}}))
    assert excinfo.value.field == "type"


def test_type_supports_only_any(module):
    with pytest.raises(UnsupportedFilter):
        _library(module, FilterQuery.model_validate({"type": {"none": ["album"]}}))


def test_the_library_pages_without_repeating_an_item(module):
    everything = _library(module, limit=200)
    seen = []
    for offset in range(0, everything.total, 3):
        seen += [item.id.id for item in _library(module, offset=offset, limit=3).items]

    assert seen == [item.id.id for item in everything.items]


def test_the_type_vocabulary_is_the_kinds_the_library_holds(module):
    values = asyncio.run(module.list_filter_values(_catalog("library"), "type"))

    assert [value.id for value in values.items] == [
        "artist",
        "album",
        "track",
        "playlist",
    ]
    assert all(value.count for value in values.items)


def test_the_sections_and_the_query_layer_name_the_same_kinds():
    """The section's kind is the key the query layer is asked for, so a kind
    added on one side and not the other is a KeyError at browse time."""
    from kalinka_plugin_localfiles import input_module_db as db
    from kalinka_plugin_localfiles import localfiles as lf

    assert {section.kind for section in lf.LIBRARY_SECTIONS} == set(db.KINDS)


def test_a_kind_named_twice_lists_its_rows_once(module):
    once = _library(module, FilterQuery.model_validate({"type": {"any": ["album"]}}))
    twice = _library(
        module, FilterQuery.model_validate({"type": {"any": ["album", "album"]}})
    )

    assert twice.total == once.total
    assert [item.id.id for item in twice.items] == [
        item.id.id for item in once.items
    ]
