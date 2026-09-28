#!/usr/bin/env python3
"""Covers taken from the album folder at scan time.

A needledrop or a download often keeps its sleeve as a plain file next to the
audio rather than tagged into it, and for a vinyl rip that scan is the most
authoritative art there is. It costs no network, so it is taken during the
scan rather than waiting behind the enricher — but only where the file itself
carried nothing, since a picture inside the file is unambiguously this
record's while a folder may hold a whole sleeve set.
"""

import io
import logging
import sqlite3
from contextlib import closing

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from PIL import Image

import kalinka_plugin_localfiles.indexer.indexer as indexer_module
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
    db = AsyncIndexerDb(config)
    return FileIndexer(config, db), db, music_dir, config


def _cover_bytes(color, size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _write_flac(path, tags, cover=None):
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    if cover is not None:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/png"
        pic.data = cover
        audio.add_picture(pic)
    audio.save()


async def _album_with(music_dir, fi, *, folder_images=(), embedded=None):
    """One tagged album folder, optionally shipping sidecar images."""
    folder = music_dir / "Nick Cave - Murder Ballads"
    folder.mkdir()
    _write_flac(
        folder / "01 Stagger Lee.flac",
        {"title": "Stagger Lee", "artist": "Nick Cave", "album": "Murder Ballads"},
        cover=embedded,
    )
    for name, size in folder_images:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (30, 90, 140)).save(target)
    changes = await fi.process_file(str(folder / "01 Stagger Lee.flac"))
    return folder, changes["albums"]


def _truncated_jpeg(size=(500, 500)):
    """A baseline JPEG cut off before its end: the header still gives its
    size, so it is chosen as the cover, but it will not decode."""
    buf = io.BytesIO()
    Image.effect_noise(size, 64).convert("RGB").save(buf, "JPEG")
    data = buf.getvalue()
    return data[: len(data) // 2]


def _failed_sources(config, entity_id):
    with closing(sqlite3.connect(config.db_path)) as conn:
        rows = conn.execute(
            "SELECT source_path FROM art_source_failures WHERE entity_id = ?",
            (entity_id,),
        )
        return [path for (path,) in rows]


def _spy_on_save(monkeypatch):
    calls = []
    real = indexer_module.save_artwork_from_file

    def spy(*args, **kwargs):
        calls.append(kwargs.get("origin"))
        return real(*args, **kwargs)

    monkeypatch.setattr(indexer_module, "save_artwork_from_file", spy)
    return calls


@pytest.mark.asyncio
async def test_a_sleeve_beside_the_audio_becomes_the_cover(indexer):
    fi, db, music_dir, _ = indexer
    _, album_id = await _album_with(
        music_dir, fi, folder_images=[("cover.jpg", (500, 500))]
    )

    assert (await db.get_album_by_id(album_id))["image_url"] is None
    counts = await fi.backfill_folder_art([str(music_dir)])

    assert counts == {"albums": 1}
    album = await db.get_album_by_id(album_id)
    assert album["image_url"] == f"{album_id}.jpg"
    assert album["image_generated"] == 0
    for suffix in ("thumbnail", "small", "large"):
        assert (fi.artwork_path / "album" / f"{album_id}_{suffix}.jpg").exists()


@pytest.mark.asyncio
async def test_a_scan_subfolder_is_searched(indexer):
    """Sleeve scans are commonly filed under PIC/ rather than beside the audio."""
    fi, db, music_dir, _ = indexer
    _, album_id = await _album_with(
        music_dir, fi, folder_images=[("PIC/sleeve_1.jpg", (2000, 2000))]
    )

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1
    assert (await db.get_album_by_id(album_id))["image_url"] == f"{album_id}.jpg"


@pytest.mark.asyncio
async def test_embedded_art_is_not_replaced(indexer):
    """A picture inside the file already speaks for this record; the folder
    pass exists for what the file left unanswered."""
    fi, db, music_dir, _ = indexer
    _, album_id = await _album_with(
        music_dir,
        fi,
        folder_images=[("cover.jpg", (500, 500))],
        embedded=_cover_bytes((250, 200, 20)),
    )

    before = await db.get_album_by_id(album_id)
    assert before["image_url"] == f"{album_id}.jpg"

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0


@pytest.mark.asyncio
async def test_a_folder_with_no_images_is_left_alone(indexer):
    fi, db, music_dir, _ = indexer
    _, album_id = await _album_with(music_dir, fi)

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0
    assert (await db.get_album_by_id(album_id))["image_url"] is None


@pytest.mark.asyncio
async def test_a_generated_placeholder_is_upgraded(indexer):
    """The sweep that re-opens placeholder albums clears image_url, but a
    cover the generator drew must not keep a real one out either way."""
    fi, db, music_dir, _ = indexer
    _, album_id = await _album_with(
        music_dir, fi, folder_images=[("cover.jpg", (500, 500))]
    )
    await db.update_album(album_id, {"image_url": album_id, "image_generated": 1})

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1
    album = await db.get_album_by_id(album_id)
    assert album["image_generated"] == 0


@pytest.mark.asyncio
async def test_the_pass_is_idempotent(indexer):
    fi, db, music_dir, _ = indexer
    await _album_with(music_dir, fi, folder_images=[("cover.jpg", (500, 500))])

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1
    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0


@pytest.mark.asyncio
async def test_an_unavailable_root_is_not_touched(indexer):
    """A folder on an unmounted drive must not be read, and must not have its
    cover cleared for being unreadable."""
    fi, db, music_dir, _ = indexer
    _, album_id = await _album_with(
        music_dir, fi, folder_images=[("cover.jpg", (500, 500))]
    )

    assert (await fi.backfill_folder_art([]))["albums"] == 0
    assert (await db.get_album_by_id(album_id))["image_url"] is None


@pytest.mark.asyncio
async def test_a_folded_inlay_is_stored_as_its_front_panel(indexer):
    """The saved cover must be the right half, not the whole spread. The two
    halves are painted differently so the stored image says which was kept.
    """
    fi, db, music_dir, _ = indexer
    folder = music_dir / "Nick Cave - Murder Ballads"
    folder.mkdir()
    _write_flac(
        folder / "01 Stagger Lee.flac",
        {"title": "Stagger Lee", "artist": "Nick Cave", "album": "Murder Ballads"},
    )
    spread = Image.new("RGB", (3110, 1692), (10, 10, 200))      # back: blue
    spread.paste(Image.new("RGB", (1555, 1692), (200, 10, 10)), (1555, 0))  # front: red
    spread.save(folder / "cover.jpg")
    changes = await fi.process_file(str(folder / "01 Stagger Lee.flac"))
    album_id = changes["albums"]

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1

    with Image.open(fi.artwork_path / "album" / f"{album_id}_large.jpg") as saved:
        # One panel, not the 1.84 spread: the right half is 1555x1692.
        assert saved.width / saved.height < 1.2
        red, _, blue = saved.convert("RGB").getpixel(
            (saved.width // 2, saved.height // 2)
        )
    assert red > 150 and blue < 100  # the front panel, not the back


@pytest.mark.asyncio
async def test_a_cover_that_will_not_decode_is_reported_once(
    indexer, caplog, monkeypatch
):
    """The header of a truncated scan passes, so the same file is chosen on
    every pass. It is named in one ERROR and then left alone, not decoded
    and reported again every fifteen minutes."""
    fi, db, music_dir, _ = indexer
    folder, album_id = await _album_with(music_dir, fi)
    cover = folder / "cover.jpg"
    cover.write_bytes(_truncated_jpeg())
    saves = _spy_on_save(monkeypatch)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0
    assert saves == [str(cover)]
    [error] = caplog.records
    assert error.levelno == logging.ERROR
    assert f"album {album_id} from {cover}: " in error.getMessage()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0
    assert saves == [str(cover)]
    assert caplog.records == []
    assert (await db.get_album_by_id(album_id))["image_url"] is None


@pytest.mark.asyncio
async def test_a_replaced_cover_is_tried_again(indexer):
    fi, db, music_dir, config = indexer
    folder, album_id = await _album_with(music_dir, fi)
    cover = folder / "cover.jpg"
    cover.write_bytes(_truncated_jpeg())
    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0
    assert _failed_sources(config, album_id) == [str(cover)]

    Image.new("RGB", (500, 500), (30, 90, 140)).save(cover)

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1
    assert (await db.get_album_by_id(album_id))["image_url"] == f"{album_id}.jpg"
    assert _failed_sources(config, album_id) == []


@pytest.mark.asyncio
async def test_a_good_image_added_beside_a_broken_one_is_taken(indexer):
    """Only the broken file is passed over, not the album: a better-named
    image that arrives later is still found."""
    fi, db, music_dir, config = indexer
    folder, album_id = await _album_with(music_dir, fi)
    (folder / "cover.jpg").write_bytes(_truncated_jpeg())
    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0

    Image.new("RGB", (600, 600), (30, 90, 140)).save(folder / "folder.jpg")

    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1
    assert _failed_sources(config, album_id) == []


@pytest.mark.asyncio
async def test_a_cover_that_could_not_be_opened_is_not_written_off(
    indexer, monkeypatch
):
    """A share that did not answer has not shown the file to be broken."""
    fi, db, music_dir, config = indexer
    folder, album_id = await _album_with(
        music_dir, fi, folder_images=[("cover.jpg", (500, 500))]
    )

    def unreachable(*_):
        raise OSError("host is down")

    monkeypatch.setattr(fi, "_save_folder_cover", unreachable)
    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 0
    assert _failed_sources(config, album_id) == []

    monkeypatch.undo()
    assert (await fi.backfill_folder_art([str(music_dir)]))["albums"] == 1


@pytest.mark.asyncio
async def test_failed_sources_go_with_their_album_and_track(indexer):
    fi, db, music_dir, config = indexer
    folder, album_id = await _album_with(music_dir, fi)
    [track] = await db.get_all_tracks()
    await db.record_art_failure(album_id, str(folder / "cover.jpg"), 1, 1)
    await db.record_art_failure(track["id"], track["file_path"], 1, 1)

    await db.delete_track(track["id"])
    await db.delete_orphaned_albums_and_artists()

    assert _failed_sources(config, album_id) == []
    assert _failed_sources(config, track["id"]) == []
