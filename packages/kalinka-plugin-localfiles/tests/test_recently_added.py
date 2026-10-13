"""Recently Added sections and filters share one arrival-ordered listing."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterQuery, UnsupportedFilter

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

TRACKS = [
    ("t_a1", "al_a", 100),
    ("t_a2", "al_a", 300),
    ("t_b1", "al_b", 200),
    ("t_single", "unknown_album", 250),
    ("t_old_single", "unknown_album", 50),
]


def _seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO artists (id, name) VALUES (?, ?)",
            [("ar", "Air"), ("ar_b", "Queen"), ("orphan", "No Files")],
        )
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id, genre, last_updated)"
            " VALUES (?, ?, ?, ?, ?)",
            # Touched by the enricher long after they arrived.
            [
                ("al_a", "Oxygène", "ar", "Electronic; Ambient", 9000),
                ("al_b", "Night", "ar_b", "Rock", 10),
                ("empty", "No Files", "orphan", None, 10000),
            ],
        )
        conn.executemany(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format,"
            " duration) VALUES (?, ?, ?, 'ar', ?, 'flac', 60)",
            [(id, id, album, f"/music/{id}.flac") for id, album, _ in TRACKS],
        )
        conn.executemany(
            "INSERT INTO library_file (file_id, current_path, first_indexed)"
            " VALUES (?, ?, ?)",
            [(id, f"/music/{id}.flac", added) for id, _, added in TRACKS],
        )
        conn.execute("UPDATE tracks SET artist_id='ar_b' WHERE id='t_b1'")


@pytest.fixture
def module(tmp_path):
    config = LocalFilesConfig(
        db_path=str(tmp_path / "test.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    asyncio.run(init_db(config.db_path))
    _seed(config.db_path)
    db = LocalFilesInputModuleDb(config)
    return LocalFilesInputModule(config, db, storage_source=_NoRoots)


class _NoRoots:
    def canonical_roots(self, locations):
        return []


def _catalog(endpoint):
    return EntityId(id=endpoint, type=EntityType.CATALOG, source="localfiles")


def _recent(module, offset=0, limit=50, endpoint="recent", filter=None):
    return asyncio.run(
        module.browse(
            _catalog(endpoint),
            offset=offset,
            limit=limit,
            filter=FilterQuery.model_validate(filter or {}),
        )
    )


def _ids(listing):
    return [item.id.id for item in listing.items]


def test_recent_card_declares_three_sections_and_filters_in_wire_format(module):
    root = asyncio.run(module.browse(_catalog("root")))
    card = next(item for item in root.items if item.id.id == "recent")
    wire = card.model_dump(mode="json")
    assert [s.id.id for s in card.sections] == [
        "recent-albums",
        "recent-tracks",
        "recent-artists",
    ]
    assert [s["name"] for s in wire["sections"]] == ["Albums", "Tracks", "Artists"]
    assert [
        s["catalog"]["preview_config"]["content_type"] for s in wire["sections"]
    ] == ["album", "track", "artist"]
    assert {f["id"] for f in wire["catalog"]["filters"]} == {"q", "genre", "type"}
    for section in wire["sections"]:
        assert {f["id"] for f in section["catalog"]["filters"]} == {"q", "genre"}


def test_newest_arrival_first_including_album_tracks_and_artists(module):
    listing = _recent(module)
    assert _ids(listing) == [
        "al_a",
        "ar",
        "t_a2",
        "t_single",
        "al_b",
        "ar_b",
        "t_b1",
        "t_a1",
        "t_old_single",
    ]
    assert listing.items[0].album is not None
    assert listing.items[1].artist is not None
    assert listing.items[2].track is not None
    assert listing.total == 9


def test_new_track_resurfaces_its_album_but_metadata_edits_do_not(module):
    db = module.db_manager
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("DELETE FROM library_file WHERE file_id='t_a2'")
        conn.execute("DELETE FROM tracks WHERE id='t_a2'")
    before = _ids(_recent(module))
    assert _ids(_recent(module, endpoint="recent-albums")) == ["al_b", "al_a"]

    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE albums SET last_updated=10000 WHERE id='al_a'")
        conn.execute("UPDATE tracks SET last_updated=10000 WHERE id='t_a1'")
        conn.execute("UPDATE artists SET last_updated=10000 WHERE id='ar_b'")
        conn.execute("UPDATE tracks SET file_path='/music/moved.flac' WHERE id='t_a1'")
    assert _ids(_recent(module)) == before

    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO tracks (id,title,album_id,artist_id,file_path,format,duration)"
            " VALUES ('t_new','New','al_a','ar','/music/new.flac','flac',60)"
        )
        conn.execute(
            "INSERT INTO library_file (file_id,current_path,first_indexed)"
            " VALUES ('t_new','/music/new.flac',400)"
        )
    assert _ids(_recent(module))[:3] == ["al_a", "ar", "t_new"]


def test_pages_hold_their_place(module):
    pages = [_recent(module, offset=offset, limit=2) for offset in range(0, 10, 2)]
    ids = [id for page in pages for id in _ids(page)]
    assert ids == _ids(_recent(module))
    assert len(set(ids)) == len(ids)
    assert {page.total for page in pages} == {9}


@pytest.mark.parametrize("kind", ["album", "track", "artist"])
@pytest.mark.parametrize(
    "narrowing", [{}, {"q": {"contains": "air"}, "genre": {"all": ["ambient"]}}]
)
def test_view_all_type_and_section_return_identical_pages(module, kind, narrowing):
    parent = _recent(module, limit=1, filter={**narrowing, "type": {"any": [kind]}})
    section = _recent(module, limit=1, endpoint=f"recent-{kind}s", filter=narrowing)
    assert parent == section
    assert all(item.id.type == EntityType(kind) for item in section.items)


def test_name_and_genre_narrow_before_paging_and_counting(module):
    narrowing = {
        "q": {"contains": "air oxygene"},
        "type": {"any": ["track"]},
        "genre": {"all": ["electronic", "ambient"]},
    }
    first = _recent(module, limit=1, filter=narrowing)
    second = _recent(module, offset=1, limit=1, filter=narrowing)
    assert _ids(first) == ["t_a2"]
    assert _ids(second) == ["t_a1"]
    assert first.total == second.total == 2
    assert not _recent(module, offset=2, limit=1, filter=narrowing).items


@pytest.mark.parametrize(
    "selector,expected",
    [
        ({"any": ["ambient", "rock"]}, ["t_a2", "t_b1", "t_a1"]),
        ({"none": ["electronic"]}, ["t_single", "t_b1", "t_old_single"]),
    ],
)
def test_genre_union_and_exclusion_apply_to_recent_tracks(module, selector, expected):
    assert (
        _ids(_recent(module, endpoint="recent-tracks", filter={"genre": selector}))
        == expected
    )


def test_recent_type_vocabulary_matches_sections_counts_and_pagination(module):
    values = asyncio.run(module.list_filter_values(_catalog("recent"), "type"))
    assert [(v.id, v.name, v.count) for v in values.items] == [
        ("album", "Albums", 2),
        ("track", "Tracks", 5),
        ("artist", "Artists", 2),
    ]
    page = asyncio.run(
        module.list_filter_values(_catalog("recent"), "type", offset=1, limit=1)
    )
    assert page.items == values.items[1:2]
    assert page.total == 3
    matched = asyncio.run(
        module.list_filter_values(_catalog("recent"), "type", q="track")
    )
    assert matched.items == values.items[1:2]
    assert matched.total == 1


def test_recent_artist_genre_includes_tracks_on_another_artists_album(module):
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute("UPDATE tracks SET album_id='al_a' WHERE id='t_b1'")
    listing = _recent(
        module, endpoint="recent-artists", filter={"genre": {"any": ["ambient"]}}
    )
    assert _ids(listing) == ["ar", "ar_b"]
    assert listing.total == 2


@pytest.mark.parametrize(
    "endpoint", ["recent", "recent-albums", "recent-tracks", "recent-artists"]
)
def test_recent_catalogs_offer_the_library_genre_vocabulary(module, endpoint):
    values = asyncio.run(
        module.list_filter_values(_catalog(endpoint), "genre", q="amb")
    )
    assert [(v.id, v.name) for v in values.items] == [("ambient", "Ambient")]


@pytest.mark.parametrize(
    "endpoint,filter",
    [
        ("recent", {"type": {"any": ["playlist"]}}),
        ("recent", {"type": {"all": ["album"]}}),
        ("recent", {"year": {"gte": 2000}}),
        ("recent-tracks", {"type": {"any": ["track"]}}),
    ],
)
def test_recent_refuses_filters_it_does_not_offer(module, endpoint, filter):
    with pytest.raises(UnsupportedFilter):
        _recent(module, endpoint=endpoint, filter=filter)


def test_multiple_types_are_deduplicated(module):
    listing = _recent(module, filter={"type": {"any": ["album", "artist", "album"]}})
    assert _ids(listing) == ["al_a", "ar", "al_b", "ar_b"]
    assert listing.total == 4


def test_no_placeholder_or_empty_entities_in_recent_sections(module):
    with sqlite3.connect(module.db_manager.db_path) as conn:
        conn.execute(
            "UPDATE tracks SET album_id='unknown_album',artist_id='unknown_artist'"
        )
    card = next(
        item
        for item in asyncio.run(module.browse(_catalog("root"))).items
        if item.id.id == "recent"
    )
    assert [s.id.id for s in card.sections] == ["recent-tracks"]
    values = asyncio.run(module.list_filter_values(_catalog("recent"), "type"))
    assert [v.id for v in values.items] == ["track"]
    assert _recent(module).total == 5
