#!/usr/bin/env python3
"""The indexer resolves a track from its tags, and from its path when the
tags do not answer.

This is the preliminary pass: by the time a scan finishes, a library of
untagged files already reads correctly, without a single network call. The
enricher then improves on it — it is no longer what makes it legible in the
first place.

Album titles are not decided here. One folder is one album, which only the
clustering pass at the end of the scan can see, so these tests run it too
wherever an album title is asserted.
"""

import aiosqlite
import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb


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


def _write(path, tags=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    if tags:
        audio = FLAC(str(path))
        for k, v in tags.items():
            audio[k] = v
        audio.save()
    return path


async def _track(config, track_id):
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT t.*, ar.name AS artist_name, al.title AS album_title "
            "FROM tracks t "
            "LEFT JOIN artists ar ON ar.id = t.artist_id "
            "LEFT JOIN albums al ON al.id = t.album_id "
            "WHERE t.id = ?",
            (track_id,),
        )
        return dict(await cur.fetchone())


async def _scan(fi, music_dir, relative, tags=None):
    """Index one file the way a scan does, clustering included."""
    changes = await fi.process_file(str(_write(music_dir / relative, tags)))
    await fi.recluster()
    return changes["tracks"]


class TestLayouts:
    """The shapes a library is actually laid out in. Each keeps a real name:
    the model has no word list, so placeholders like "Artist - Album" are
    adversarial in a way no real path is."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "relative,artist,album",
        [
            ("Murder Ballads/Stagger Lee.flac",
             "Unknown Artist", "Murder Ballads"),
            ("Nick Cave/Murder Ballads/Stagger Lee.flac",
             "Nick Cave", "Murder Ballads"),
            ("Nick Cave - Murder Ballads/Stagger Lee.flac",
             "Nick Cave", "Murder Ballads"),
            (
                "Rock/Nick Cave/Murder Ballads/Stagger Lee.flac",
                "Nick Cave",
                "Murder Ballads",
            ),
        ],
        ids=["album only", "artist/album", "artist - album", "genre/artist/album"],
    )
    async def test_an_untagged_file_is_named_by_its_path(
        self, indexer, relative, artist, album
    ):
        fi, music_dir, config = indexer
        row = await _track(config, await _scan(fi, music_dir, relative))
        assert row["title"] == "Stagger Lee"
        assert row["artist_name"] == artist
        assert row["album_title"] == album

    @pytest.mark.asyncio
    async def test_a_file_at_the_root_has_only_its_own_name_to_go_on(self, indexer):
        """No directory above it names anything, so only the title survives.

        The root is a storage container, so neither the parser nor clustering
        may invent an album from its name.
        """
        fi, music_dir, config = indexer
        row = await _track(config, await _scan(fi, music_dir, "Stagger Lee.flac"))
        # And not "Stagger Lee.flac": the old fallback wrote the bare
        # basename, extension and all.
        assert row["title"] == "Stagger Lee"
        assert row["artist_name"] == "Unknown Artist"
        assert row["album_id"] == "unknown_album"


class TestTagsWin:
    @pytest.mark.asyncio
    async def test_a_tagged_file_keeps_its_tags(self, indexer):
        fi, music_dir, config = indexer
        track_id = await _scan(
            fi,
            music_dir,
            "Nick Cave/Murder Ballads/Stagger Lee.flac",
            {"title": "Stagger Lee (Live)", "artist": "Nick Cave & The Bad Seeds",
             "album": "The Abattoir Blues Tour", "tracknumber": "7"},
        )
        row = await _track(config, track_id)
        assert row["title"] == "Stagger Lee (Live)"
        assert row["artist_name"] == "Nick Cave & The Bad Seeds"
        assert row["album_title"] == "The Abattoir Blues Tour"
        assert row["track_number"] == 7

    @pytest.mark.asyncio
    async def test_a_fully_tagged_file_is_never_parsed(self, indexer, monkeypatch):
        """The model is not consulted when there is nothing left to answer."""
        fi, music_dir, _ = indexer
        import kalinka_plugin_localfiles.indexer.indexer as mod

        def _boom(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("the path was parsed for a fully tagged file")

        monkeypatch.setattr(mod, "parse_music_path", _boom)
        await fi.process_file(
            str(_write(
                music_dir / "Nick Cave/Murder Ballads/Stagger Lee.flac",
                {"title": "Stagger Lee", "artist": "Nick Cave",
                 "album": "Murder Ballads", "tracknumber": "5", "date": "1996"},
            ))
        )

    @pytest.mark.asyncio
    async def test_one_missing_field_is_enough_to_parse(self, indexer):
        """Tagged but for the track number: the path supplies only that."""
        fi, music_dir, config = indexer
        row = await _track(config, await _scan(
            fi,
            music_dir,
            "Nick Cave/Murder Ballads/03 - Stagger Lee.flac",
            {"title": "Stagger Lee", "artist": "Nick Cave", "album": "Murder Ballads"},
        ))
        assert row["track_number"] == 3
        assert row["title"] == "Stagger Lee"


class TestNumbersAndYear:
    @pytest.mark.asyncio
    async def test_a_disc_subdirectory_supplies_the_disc_and_the_year(self, indexer):
        fi, music_dir, config = indexer
        row = await _track(config, await _scan(
            fi, music_dir,
            "Pink Floyd/1979 - The Wall/CD2/05 - Comfortably Numb.flac",
        ))
        assert row["title"] == "Comfortably Numb"
        assert row["artist_name"] == "Pink Floyd"
        assert (row["track_number"], row["disc_number"]) == (5, 2)

    @pytest.mark.asyncio
    async def test_the_year_lands_on_the_album(self, indexer):
        fi, music_dir, config = indexer
        await _scan(
            fi, music_dir,
            "Pink Floyd/1979 - The Wall/CD2/05 - Comfortably Numb.flac",
        )
        async with aiosqlite.connect(config.db_path) as conn:
            cur = await conn.execute(
                "SELECT year FROM albums WHERE id != 'unknown_album'"
            )
            assert [r[0] for r in await cur.fetchall()] == [1979]
