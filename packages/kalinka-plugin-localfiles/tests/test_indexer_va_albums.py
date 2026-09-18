#!/usr/bin/env python3
"""Tests for V/A folder coalescing in the indexer.

A folder whose tracks span many artists is collapsed into one album:
  * a real compilation -> a Various-Artists album (folder-name title),
  * a folder under a real artist (remix album) -> that artist's album,
  * a generic dump folder -> left loose under unknown_album.
In every case each track keeps its real artist and stays navigable via the
orphan/appears-on query.
"""

import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.clustering.classify import (
    compilation_title,
    strip_artist_prefix,
)
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.id_generator import generate_artist_id
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb


@pytest_asyncio.fixture
async def indexer(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music_dir)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    return FileIndexer(config, AsyncIndexerDb(config)), music_dir, config


def _meta(**over):
    base = {"format": "audio/mpeg", "duration": 100}
    base.update(over)
    return base


async def _index_folder(fi, folder, artists):
    """Index one file per artist into ``folder``, tagging each with that
    artist (process_file runs _extract_metadata in a thread, so a plain
    function bound per-iteration is fine)."""
    folder.mkdir(parents=True, exist_ok=True)
    for i, artist in enumerate(artists):
        fpath = folder / f"{i:02d} - {artist} - Song {i}.mp3"
        fpath.write_bytes(b"x")
        fi._extract_metadata = lambda _s, _p, a=artist, n=i: _meta(
            artist=a, title=f"Song {n}"
        )
        await fi.process_file(str(fpath))


@pytest.mark.asyncio
async def test_va_folder_becomes_compilation_album(indexer):
    fi, music_dir, config = indexer
    comp = music_dir / "VA - Future Trance Best Of"
    artists = ["Scooter", "Mayday", "DJ Shog", "Ziggy X", "Sonique"]
    await _index_folder(fi, comp, artists)

    res = await fi.orphan_va_folder_tracks()
    assert res["folders"] == 1

    idb = LocalFilesInputModuleDb(config)
    albums, total = idb.get_artist_albums("various_artists", 0, 50)
    assert total == 1
    assert albums[0]["title"] == "Future Trance Best Of"  # "VA - " prefix stripped

    # A contributor still reaches their track via the orphan/appears-on path.
    scooter = await fi.db_manager.get_artist_by_id(generate_artist_id("Scooter"))
    _, n = idb.get_artist_orphan_tracks(scooter["id"], 0, 50)
    assert n >= 1


@pytest.mark.asyncio
async def test_remix_folder_under_artist_is_attributed_to_that_artist(indexer):
    """A ".../Netsky/Netsky Remixes" folder of remixer-credited tracks is that
    artist's release, not a Various-Artists compilation — it should be owned by
    Netsky (who has a catalog in another folder), with the redundant artist
    prefix stripped from the title."""
    fi, music_dir, config = indexer
    netsky = music_dir / "Netsky"
    await _index_folder(fi, netsky / "Album", ["Netsky", "Netsky"])  # catalog elsewhere
    await _index_folder(
        fi, netsky / "Netsky Remixes", ["Rmx A", "Rmx B", "Rmx C", "Rmx D", "Rmx E"]
    )

    await fi.orphan_va_folder_tracks()

    idb = LocalFilesInputModuleDb(config)
    netsky_id = generate_artist_id("Netsky")
    albums, _ = idb.get_artist_albums(netsky_id, 0, 50)
    titles = {a["title"] for a in albums}
    assert (
        "Remixes" in titles
    )  # "Netsky Remixes" -> stripped to "Remixes", under Netsky
    # And it is NOT a Various-Artists album.
    va_albums = idb.get_artist_albums("various_artists", 0, 50)[0]
    assert all(a["title"] != "Remixes" for a in va_albums)


@pytest.mark.asyncio
async def test_generic_dump_folder_stays_unknown_album(indexer):
    fi, music_dir, config = indexer
    dump = music_dir / "90s Mixes"
    artists = ["A One", "B Two", "C Three", "D Four", "E Five"]
    await _index_folder(fi, dump, artists)

    await fi.orphan_va_folder_tracks()

    import sqlite3

    con = sqlite3.connect(config.db_path)
    va = con.execute(
        "SELECT COUNT(*) FROM albums WHERE artist_id='various_artists'"
    ).fetchone()[0]
    unk = con.execute(
        "SELECT COUNT(*) FROM tracks WHERE album_id='unknown_album'"
    ).fetchone()[0]
    con.close()
    assert va == 0  # no fabricated compilation album
    assert unk == len(artists)  # tracks left loose


def test_strip_artist_prefix_requires_a_boundary():
    f = strip_artist_prefix
    assert f("Ratatat Remixes Vol. 2", "Ratatat") == "Remixes Vol. 2"
    assert f("Massive Attack - Sessions", "Massive Attack") == "Sessions"
    assert f("Remixes", "Netsky") == "Remixes"  # no prefix -> unchanged
    assert f("ABBA Gold", "AB") == "ABBA Gold"  # boundary guard, not "BA Gold"


@pytest.mark.asyncio
async def test_va_marked_folder_bypasses_generic_filter(indexer):
    """An explicit "VA -" marker declares a compilation, so a name that would
    otherwise be treated as a generic dump (e.g. "Mixes") is still honoured."""
    fi, music_dir, _ = indexer
    assert compilation_title(str(music_dir / "VA - Trance Mixes")) == "Trance Mixes"
    assert compilation_title(str(music_dir / "90s Mixes")) is None  # no marker
