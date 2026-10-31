#!/usr/bin/env python3
"""End-to-end: a tagless single-file CD rip with a sibling .cue is indexed with
the artist/album/year taken from the cue (not the mangled folder name), and the
parsed tracklist is stored as evidence.
"""

import aiosqlite
import json
import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb

CUE = """REM GENRE "Synth-pop"
REM DATE "1982"
PERFORMER "Roxy Music"
TITLE "Avalon"
FILE "1982 - Roxy Music - Avalon.flac" WAVE
  TRACK 01 AUDIO
    TITLE "More Than This"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Avalon"
    INDEX 01 04:30:00
"""


@pytest_asyncio.fixture
async def indexer(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    return FileIndexer(config, AsyncIndexerDb(config)), music, config


async def _row(config, sql, params=()):
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        r = await (await conn.execute(sql, params)).fetchone()
        return dict(r) if r else None


@pytest.mark.asyncio
async def test_cue_supplies_artist_album_for_tagless_rip(indexer):
    fi, music, config = indexer
    folder = music / "1982 - Roxy Music - Avalon (EG, UK)"
    folder.mkdir(parents=True)
    flac = folder / "1982 - Roxy Music - Avalon.flac"
    # A tagless FLAC — everything must come from the cue, not the folder name.
    sf.write(str(flac), np.zeros(2205, dtype="float32"), 44100, format="FLAC")
    (folder / "1982 - Roxy Music - Avalon.cue").write_bytes(CUE.encode("utf-16"))

    await fi.process_file(str(flac))

    row = await _row(
        config,
        """SELECT ar.name AS artist, al.title AS album, al.year AS year,
                  t.id AS tid
           FROM tracks t
           JOIN artists ar ON t.artist_id = ar.id
           JOIN albums al ON t.album_id = al.id""",
    )
    assert row["artist"] == "Roxy Music"   # not "1982"
    assert row["album"] == "Avalon"        # not "Roxy Music - Avalon"
    assert row["year"] == 1982

    ev = await _row(
        config,
        "SELECT cue_sheet, cue_tracks FROM track_evidence WHERE track_id=?",
        (row["tid"],),
    )
    assert ev["cue_sheet"].endswith("Avalon.cue")
    cue = json.loads(ev["cue_tracks"])
    assert cue["album"] == "Avalon"
    assert [t["title"] for t in cue["tracks"]] == ["More Than This", "Avalon"]

    # The cue-derived album title must survive a folder-first recluster pass,
    # which otherwise renames the album from the folder ("...Avalon (EG, UK)").
    await fi.recluster()
    after = await _row(
        config, "SELECT title FROM albums WHERE id=?", (
            (await _row(config, "SELECT album_id FROM tracks WHERE id=?", (row["tid"],)))[
                "album_id"
            ],
        )
    )
    assert after["title"] == "Avalon"


MULTI_FILE_CUE = """PERFORMER "Roxy Music"
TITLE "Avalon"
FILE "01 - More Than This.flac" WAVE
  TRACK 01 AUDIO
    TITLE "More Than This"
    INDEX 01 00:00:00
FILE "02 - The Space Between.flac" WAVE
  TRACK 02 AUDIO
    TITLE "The Space Between"
    INDEX 01 00:00:00
"""


@pytest.mark.asyncio
async def test_multi_file_cue_uses_per_track_title_and_number(indexer):
    # A cue with a FLAC per track: each file must get its own title + number,
    # not the disc title (the whole-disc-blob case is the other test).
    fi, music, config = indexer
    folder = music / "Roxy Music - Avalon"
    folder.mkdir(parents=True)
    for name in ("01 - More Than This.flac", "02 - The Space Between.flac"):
        sf.write(str(folder / name), np.zeros(2205, dtype="float32"), 44100,
                 format="FLAC")
    (folder / "album.cue").write_bytes(MULTI_FILE_CUE.encode("utf-8"))

    for name in ("01 - More Than This.flac", "02 - The Space Between.flac"):
        await fi.process_file(str(folder / name))

    rows = {}
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT title, track_number FROM tracks ORDER BY track_number"
        )
        for r in await cur.fetchall():
            rows[r["track_number"]] = r["title"]
    assert rows == {1: "More Than This", 2: "The Space Between"}


@pytest.mark.asyncio
async def test_embedded_tags_win_over_cue(indexer):
    fi, music, config = indexer
    folder = music / "Album"
    folder.mkdir(parents=True)
    flac = folder / "track.flac"
    sf.write(str(flac), np.zeros(2205, dtype="float32"), 44100, format="FLAC")
    # Give the FLAC real tags; the cue must not override them.
    from mutagen.flac import FLAC

    a = FLAC(str(flac))
    a["artist"] = "Real Artist"
    a["album"] = "Real Album"
    a.save()
    (folder / "track.cue").write_bytes(CUE.encode("utf-8"))

    await fi.process_file(str(flac))
    row = await _row(
        config,
        """SELECT ar.name AS artist, al.title AS album
           FROM tracks t JOIN artists ar ON t.artist_id=ar.id
           JOIN albums al ON t.album_id=al.id""",
    )
    assert row["artist"] == "Real Artist"
    assert row["album"] == "Real Album"


def _cue(title, performer, file_name):
    return (
        f'PERFORMER "{performer}"\nTITLE "{title}"\nFILE "{file_name}" WAVE\n'
        '  TRACK 01 AUDIO\n    TITLE "One"\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    TITLE "Two"\n    INDEX 01 00:00:30\n'
    )


async def _grouped_rips(fi, config, folder, titles):
    """Index one untagged whole-disc rip per cue title, regroup, and return
    each rip's album id."""
    folder.mkdir(exist_ok=True)
    for title in titles:
        flac = folder / f"{title}.flac"
        sf.write(str(flac), np.zeros(2205, dtype="float32"), 44100, format="FLAC")
        (folder / f"{title}.cue").write_text(_cue(title, "Roxy Music", flac.name))
        await fi.process_file(str(flac))
    await fi._group_albums()
    async with aiosqlite.connect(config.db_path) as conn:
        rows = await (
            await conn.execute("SELECT album_id FROM tracks ORDER BY file_path")
        ).fetchall()
    return [album for (album,) in rows]


@pytest.mark.asyncio
async def test_untagged_cue_albums_sharing_a_folder_stay_apart(indexer):
    fi, music, config = indexer
    albums = await _grouped_rips(
        fi, config, music / "Rips", ["Avalon", "Flesh and Blood"]
    )
    assert len(set(albums)) == 2


@pytest.mark.parametrize(
    "titles",
    [
        ["Mellon Collie - Disc One", "Mellon Collie - Disc Two"],
        ["Mellon Collie CD-1", "Mellon Collie CD-2"],
        ["Mellon Collie [CD 1 of 2]", "Mellon Collie [CD 2 of 2]"],
    ],
)
@pytest.mark.asyncio
async def test_the_discs_of_one_set_stay_together(indexer, titles):
    fi, music, config = indexer
    albums = await _grouped_rips(fi, config, music / "Mellon Collie", titles)
    assert len(set(albums)) == 1
    title = await _row(config, "SELECT title FROM albums WHERE id = ?", (albums[0],))
    assert title["title"] == "Mellon Collie"


@pytest.mark.asyncio
async def test_the_discs_of_one_set_stay_together_at_the_library_root(indexer):
    fi, music, config = indexer
    albums = await _grouped_rips(
        fi, config, music, ["Mellon Collie - Disc One", "Mellon Collie - Disc Two"]
    )
    assert len(set(albums)) == 1
