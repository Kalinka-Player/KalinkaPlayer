"""Browsing the library by folder: the tree is the indexed tracks' paths,
under the configured music sources, and nothing else."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from kalinka_plugin_sdk.datamodel import EntityId, EntityType, PreviewType
from kalinka_plugin_sdk.filters import FilterQuery, UnsupportedFilter

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles import input_module_db
from kalinka_plugin_localfiles.folder_ids import decode_folder_id, encode_folder_id
from kalinka_plugin_localfiles.input_module_db import (
    FolderSummary,
    LocalFilesInputModuleDb,
)
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.utils import mount_status
from kalinka_plugin_localfiles.utils.mount_status import Mount

TRACKS = [
    ("t_loose", "/lib/music/loose.flac", "unknown_album"),
    ("t_b", "/lib/music/b/one.flac", "al_b"),
    ("t_cafe", "/lib/music/Café/x.flac", "al_b"),
    ("t_a11", "/lib/music/a/CD1/01.flac", "al_a"),
    ("t_a12", "/lib/music/a/CD1/02.flac", "al_a"),
    ("t_a21", "/lib/music/a/CD2/01.flac", "al_a"),
    ("t_notes", "/lib/music/a/Notes.flac", "al_notes"),
    ("t_alpha", "/lib/music/a/alpha.flac", "al_notes"),
    ("t_odd", "/lib/music/100%_done/z.flac", "al_b"),
    ("t_sibling", "/lib/music2/other.flac", "al_b"),
    ("t_out", "/elsewhere/out.flac", "al_b"),
    ("t_smb", "smb://nas/music/x/y.flac", "al_b"),
]


def _seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO artists (id, name) VALUES ('ar', 'Artist')")
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id) VALUES (?, ?, 'ar')",
            [("al_a", "A"), ("al_b", "B"), ("al_notes", "Notes")],
        )
        conn.executemany(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format,"
            " duration) VALUES (?, ?, ?, 'ar', ?, 'flac', 600)",
            [(id, id, album, path) for id, path, album in TRACKS],
        )


class _Roots:
    def __init__(self, roots):
        self._roots = roots

    def canonical_roots(self, locations):
        return list(self._roots)


@pytest.fixture
def config(tmp_path):
    cfg = LocalFilesConfig(
        db_path=str(tmp_path / "test.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    asyncio.run(init_db(cfg.db_path))
    _seed(cfg.db_path)
    return cfg


@pytest.fixture
def db(config):
    return LocalFilesInputModuleDb(config)


def _module(config, db, roots):
    return LocalFilesInputModule(config, db, storage_source=lambda: _Roots(roots))


@pytest.fixture
def module(config, db):
    return _module(config, db, ["/lib/music", "/lib/music2", "/empty"])


def _browse(module, id, offset=0, limit=50, filter=None):
    return asyncio.run(module.browse(id, offset=offset, limit=limit, filter=filter))


def _catalog(endpoint):
    return EntityId(id=endpoint, type=EntityType.CATALOG, source="localfiles")


def _folder(path):
    return _catalog("folder." + encode_folder_id(path))


def _path_of(item):
    return decode_folder_id(item.id.id.removeprefix("folder."))


def test_a_folder_lists_its_subfolders_by_name_then_its_tracks(module):
    listing = _browse(module, _folder("/lib/music"))

    assert [item.name for item in listing.items] == [
        "100%_done",
        "a",
        "b",
        "Café",
        "t_loose",
    ]
    assert listing.total == 5
    assert [_path_of(item) for item in listing.items[:4]] == [
        "/lib/music/100%_done",
        "/lib/music/a",
        "/lib/music/b",
        "/lib/music/Café",
    ]
    assert listing.items[4].id == EntityId.from_string(
        "kalinka:localfiles:track:t_loose"
    )


def test_a_sibling_whose_name_extends_the_folders_is_not_inside_it(module):
    names = {item.name for item in _browse(module, _folder("/lib/music")).items}

    assert "other" not in names and "t_sibling" not in names


def test_pages_cross_from_folders_to_tracks_under_one_total(module):
    pages = [_browse(module, _folder("/lib/music"), offset, 2) for offset in (0, 2, 4)]

    assert [[item.name for item in page.items] for page in pages] == [
        ["100%_done", "a"],
        ["b", "Café"],
        ["t_loose"],
    ]
    assert {page.total for page in pages} == {5}


def test_tracks_directly_in_a_folder_sort_by_file_name_regardless_of_case(module):
    listing = _browse(module, _folder("/lib/music/a"))

    assert [item.name for item in listing.items] == ["CD1", "CD2", "t_alpha", "t_notes"]


def test_a_folder_says_what_is_directly_in_it_and_how_much_is_below(module):
    a = _browse(module, _folder("/lib/music")).items[1]

    assert a.name == "a"
    assert a.subname == "5 tracks"
    assert a.catalog.description == "2 folders · 2 tracks · 20 min"
    assert a.catalog.preview_config.type is PreviewType.FOLDER
    assert a.catalog.filters == []
    assert a.can_browse and a.can_add


def test_a_folder_holding_only_folders_can_still_be_added(config, db):
    module = _module(config, db, ["/"])

    lib = next(
        item for item in _browse(module, _folder("/")).items if item.name == "lib"
    )

    assert lib.can_add
    assert lib.catalog.description == "2 folders"


def test_a_library_of_several_sources_opens_on_each_that_holds_tracks(module):
    listing = _browse(module, _catalog("files"))

    assert [item.name for item in listing.items] == ["music", "music2"]
    assert [_path_of(item) for item in listing.items] == ["/lib/music", "/lib/music2"]
    assert listing.total == 2


def test_each_source_says_what_it_is_and_where(config, db, monkeypatch):
    mounts = [
        Mount("/", "ext4", "/dev/sda1"),
        Mount("/lib/music2", "cifs", "//nas/music"),
        Mount("/elsewhere", "fuse.sshfs", "me@host:/srv"),
    ]
    monkeypatch.setattr(mount_status, "list_mounts", lambda: mounts)
    roots = ["/lib/music", "/lib/music2", "/elsewhere", "smb://nas/music"]
    module = _module(config, db, roots)

    listing = _browse(module, _catalog("files"))

    assert [item.subname for item in listing.items] == [
        "On this server · /lib/music",
        "Mounted SMB share · /lib/music2",
        "Mounted network share · /elsewhere",
        "SMB share on nas · /music",
    ]


def test_a_library_of_one_source_opens_on_its_own_folders(config, db):
    module = _module(config, db, ["/lib/music"])

    listing = _browse(module, _catalog("files"))

    assert [item.name for item in listing.items] == [
        "100%_done",
        "a",
        "b",
        "Café",
        "t_loose",
    ]


def test_the_library_is_never_added_whole(module):
    card = _browse(module, _catalog("root")).items[0]

    assert card.id.id == "files"
    assert not card.can_add
    assert _adds(module, _catalog("files")) == []


def test_a_share_is_browsed_like_a_folder(config, db):
    module = _module(config, db, ["smb://nas/music"])

    [x] = _browse(module, _catalog("files")).items

    assert _path_of(x) == "smb://nas/music/x"
    assert [item.name for item in _browse(module, x.id).items] == ["t_smb"]


def test_the_filesystem_root_as_a_source_does_not_double_its_separator(config, db):
    module = _module(config, db, ["/"])

    children = _browse(module, _catalog("files")).items

    assert [_path_of(item) for item in children] == ["/elsewhere", "/lib"]


def test_a_folder_outside_every_source_lists_nothing(module):
    assert _browse(module, _folder("/elsewhere")).items == []
    assert _browse(module, _folder("/lib")).items == []


def test_an_id_naming_no_folder_lists_nothing(module):
    listing = _browse(module, _catalog("folder.not+base64"))

    assert listing.items == [] and listing.total == 0


def test_a_folder_refuses_a_filter_it_never_offered(module):
    with pytest.raises(UnsupportedFilter):
        _browse(
            module,
            _folder("/lib/music"),
            filter=FilterQuery.model_validate({"q": {"contains": "a"}}),
        )


def test_the_library_card_browses_in_the_folder_layout(module):
    library, recent = _browse(module, _catalog("root")).items

    assert library.id == _catalog("files")
    assert library.catalog.preview_config.type is PreviewType.FOLDER
    assert library.sections is None
    assert recent.id == _catalog("recent")
    assert recent.catalog.preview_config.type is not PreviewType.FOLDER


def test_summary_counts_direct_children_and_every_track_below(db):
    assert db.get_folder_summary("/lib/music/a") == FolderSummary(
        direct_folders=2, direct_tracks=2, direct_duration=1200, total_tracks=5
    )
    assert db.get_folder_summary("/nowhere") == FolderSummary(0, 0, 0, 0)


def test_a_percent_or_underscore_in_a_name_is_literal(db):
    rows, total = db.list_folder_tracks("/lib/music/100%_done")

    assert [row["id"] for row in rows] == ["t_odd"] and total == 1
    assert db.list_folder_tracks("/lib/music/100")[1] == 0


def test_the_folder_queries_are_served_by_the_path_index(config):
    with sqlite3.connect(config.db_path) as conn:
        names = [row[1] for row in conn.execute("PRAGMA index_list(tracks)")]
    assert "idx_tracks_file_path" in names


def _adds(module, entity_id, limit=50):
    ids = asyncio.run(module.tracks_to_add(entity_id, limit))
    return None if ids is None else [id.id for id in ids]


def test_a_folder_adds_everything_below_it_in_the_order_it_is_listed(module):
    assert _adds(module, _folder("/lib/music")) == [
        "t_odd",
        "t_a11",
        "t_a12",
        "t_a21",
        "t_alpha",
        "t_notes",
        "t_b",
        "t_cafe",
        "t_loose",
    ]


def test_each_source_adds_on_its_own(module):
    [music, music2] = _browse(module, _catalog("files")).items

    assert music.can_add and music2.can_add
    assert len(_adds(module, music.id)) == 9
    assert _adds(module, music2.id) == ["t_sibling"]


def test_a_large_folder_walked_a_subfolder_at_a_time_keeps_that_order(
    module, monkeypatch
):
    in_one_go = _adds(module, _folder("/lib/music"))
    monkeypatch.setattr(input_module_db, "_ORDERED_IN_ONE", 1)

    assert _adds(module, _folder("/lib/music")) == in_one_go
    assert _adds(module, _folder("/lib/music"), limit=4) == in_one_go[:4]


def test_what_a_folder_adds_stops_at_the_limit(module):
    assert _adds(module, _folder("/lib/music/a"), limit=2) == ["t_a11", "t_a12"]


def test_a_folder_outside_every_source_adds_nothing(module):
    assert _adds(module, _folder("/elsewhere")) == []
    assert _adds(module, _catalog("folder.%%")) == []


def test_other_catalogs_add_what_they_list(module):
    assert _adds(module, _catalog("recent")) is None
    assert (
        _adds(module, EntityId(id="al_a", type=EntityType.ALBUM, source="localfiles"))
        is None
    )
