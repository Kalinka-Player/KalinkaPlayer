#!/usr/bin/env python3
"""Singles on unknown_album have no album row to carry their embedded cover,
so they get track-level art: saved at index time when the track lands there
directly, and backfilled from the file after clustering demotes it and drops
its per-file album row.
"""

import io
import os
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
from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule


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


LOCAL = LocalStorage()


def _cover_png(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
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


@pytest.mark.asyncio
async def test_untagged_single_gets_track_art_at_index_time(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "loose.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast"},
                cover=_cover_png((250, 200, 20)))

    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]

    track = await db.get_track_by_id(track_id)
    assert track["album_id"] == "unknown_album"
    assert track["image_url"] == f"{track_id}.jpg"
    assert (fi.artwork_path / "track" / f"{track_id}_thumbnail.jpg").exists()


@pytest.mark.asyncio
async def test_new_album_gets_no_cover_from_a_picture_that_would_not_save(
    indexer,
):
    """A cover reference to files never written would also keep the album
    out of every pass that looks for one."""
    fi, db, music_dir, config = indexer
    path = music_dir / "bee.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast",
                       "album": "Bee Moved"},
                cover=b"not an image")

    track_id = (await fi.process_file(str(path)))["tracks"]

    album_id = (await db.get_track_by_id(track_id))["album_id"]
    assert album_id != "unknown_album"
    assert (await db.get_album_by_id(album_id))["image_url"] is None
    assert not (fi.artwork_path / "album" / f"{album_id}_large.jpg").exists()


@pytest.mark.asyncio
async def test_reindex_without_embedded_art_drops_the_track_cover(indexer):
    """The update is surgical, so a cover the file no longer carries would
    otherwise stay on the row forever."""
    fi, db, music_dir, config = indexer
    path = music_dir / "loose.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast"},
                cover=_cover_png((250, 200, 20)))
    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]
    assert (await db.get_track_by_id(track_id))["image_url"] == f"{track_id}.jpg"

    path.unlink()
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast"})
    await fi.process_file(str(path))

    assert (await db.get_track_by_id(track_id))["image_url"] is None


@pytest.mark.asyncio
async def test_backfill_restores_art_after_clustering_demotion(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "single.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast",
                       "album": "Bee Moved"},
                cover=_cover_png((20, 90, 250)))

    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]
    track = await db.get_track_by_id(track_id)
    assert track["album_id"] != "unknown_album"
    assert track["image_url"] is None

    # What recluster does to a V/A dump folder: the track goes to
    # unknown_album and the per-file album row is orphan-deleted.
    await db.reassign_album(track_id, "unknown_album")
    await db.delete_orphaned_albums_and_artists()

    saved = await fi.backfill_embedded_art([str(music_dir)])
    assert saved == {"albums": 0, "tracks": 1}
    track = await db.get_track_by_id(track_id)
    assert track["image_url"] == f"{track_id}.jpg"
    assert (fi.artwork_path / "track" / f"{track_id}_large.jpg").exists()

    # Idempotent: a second pass finds nothing to do.
    assert await fi.backfill_embedded_art([str(music_dir)]) == {
        "albums": 0,
        "tracks": 0,
    }


NOTHING = {"albums": 0, "tracks": 0}
TAGS = {"title": "Bee Moved", "artist": "Blue Coast"}


def _truncated_jpeg():
    buf = io.BytesIO()
    Image.effect_noise((400, 400), 64).convert("RGB").save(buf, "JPEG")
    data = buf.getvalue()
    return data[: len(data) // 2]


def _failed_sources(config, entity_id):
    with closing(sqlite3.connect(config.db_path)) as conn:
        rows = conn.execute(
            "SELECT source_path FROM art_source_failures WHERE entity_id = ?",
            (entity_id,),
        )
        return {path for (path,) in rows}


def _touch(path):
    """Change the file's mtime past the filesystem's timestamp granularity,
    which a fast test can otherwise fall inside."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


async def _single_whose_cover_broke(fi, db, music_dir):
    """A loose single indexed with a good cover, then re-tagged with one cut
    short. The broken picture has no hash, so the good one's is kept, and
    the backfill goes on selecting the track."""
    path = music_dir / "loose.flac"
    _write_flac(path, TAGS, cover=_cover_png((20, 90, 250)))
    track_id = (await fi.process_file(str(path)))["tracks"]
    _write_flac(path, TAGS, cover=_truncated_jpeg())
    await fi.process_file(str(path))
    assert (await db.get_track_by_id(track_id))["image_url"] is None
    return path, track_id


async def _single_needing_its_cover(fi, db, music_dir):
    path = music_dir / "loose.flac"
    _write_flac(path, TAGS, cover=_cover_png((20, 90, 250)))
    track_id = (await fi.process_file(str(path)))["tracks"]
    await db.update_track(track_id, {"image_url": None})
    return path, track_id


class _DroppingFile(io.BytesIO):
    """A file on a share that stops answering after its first few bytes."""

    def read(self, size=-1):
        if size is None or size < 0 or self.tell() + size > 64:
            raise OSError("host is down")
        return super().read(size)


@pytest.mark.asyncio
async def test_a_cover_that_will_not_decode_is_retried_once_its_file_changes(
    indexer, monkeypatch
):
    fi, db, music_dir, config = indexer
    path, track_id = await _single_whose_cover_broke(fi, db, music_dir)
    stores = []
    real_store = indexer_module.store_artwork_images

    def store(*args, **kwargs):
        stores.append(kwargs["origin"])
        return real_store(*args, **kwargs)

    monkeypatch.setattr(indexer_module, "store_artwork_images", store)

    assert await fi.backfill_embedded_art([str(music_dir)]) == NOTHING
    assert await fi.backfill_embedded_art([str(music_dir)]) == NOTHING
    assert stores == [str(path)]
    assert _failed_sources(config, track_id) == {str(path)}

    audio = FLAC(str(path))
    audio["comment"] = "remastered"
    audio.save()
    _touch(path)

    assert await fi.backfill_embedded_art([str(music_dir)]) == NOTHING
    assert stores == [str(path)] * 2


@pytest.mark.asyncio
async def test_tags_that_will_not_parse_are_not_read_again(indexer, monkeypatch):
    fi, db, music_dir, config = indexer
    path, track_id = await _single_needing_its_cover(fi, db, music_dir)
    path.write_bytes(b"no longer a FLAC file " * 64)
    reads = []
    real_read = fi._embedded_art

    def read(storage, file_path):
        reads.append(file_path)
        return real_read(storage, file_path)

    monkeypatch.setattr(fi, "_embedded_art", read)

    assert await fi.backfill_embedded_art([str(music_dir)]) == NOTHING
    assert await fi.backfill_embedded_art([str(music_dir)]) == NOTHING
    assert reads == [str(path)]
    assert _failed_sources(config, track_id) == {str(path)}


@pytest.mark.asyncio
async def test_a_file_that_could_not_be_read_is_tried_again(indexer, monkeypatch):
    """A share that stopped answering has not shown the tags to be broken,
    though the tag reader reports it as if it had."""
    fi, db, music_dir, config = indexer
    path, track_id = await _single_needing_its_cover(fi, db, music_dir)
    real_open = LocalStorage.open

    def open_(self, file_path, *, read_ahead=True):
        with real_open(self, file_path, read_ahead=read_ahead) as handle:
            return _DroppingFile(handle.read())

    monkeypatch.setattr(LocalStorage, "open", open_)
    assert await fi.backfill_embedded_art([str(music_dir)]) == NOTHING
    assert _failed_sources(config, track_id) == set()

    monkeypatch.undo()
    assert await fi.backfill_embedded_art([str(music_dir)]) == {
        "albums": 0,
        "tracks": 1,
    }


@pytest.mark.asyncio
async def test_a_written_off_cover_is_forgotten_once_the_file_is_read_again(
    indexer,
):
    fi, db, music_dir, config = indexer
    path, track_id = await _single_whose_cover_broke(fi, db, music_dir)
    await fi.backfill_embedded_art([str(music_dir)])
    assert _failed_sources(config, track_id) == {str(path)}

    _write_flac(path, TAGS, cover=_cover_png((20, 90, 250)))
    await fi.process_file(str(path))

    assert _failed_sources(config, track_id) == set()
    assert (await db.get_track_by_id(track_id))["image_url"] == f"{track_id}.jpg"


@pytest.mark.asyncio
async def test_a_written_off_cover_is_forgotten_when_the_track_joins_an_album(
    indexer,
):
    fi, db, music_dir, config = indexer
    path, track_id = await _single_whose_cover_broke(fi, db, music_dir)
    await fi.backfill_embedded_art([str(music_dir)])

    await db.reassign_album(track_id, "album_minted1")

    assert _failed_sources(config, track_id) == set()


@pytest.mark.asyncio
async def test_backfill_gives_minted_album_its_embedded_cover(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "bee.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Monday FM",
                       "album": "Bee Moved"},
                cover=_cover_png((240, 180, 0)))
    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]
    old_album = (await db.get_track_by_id(track_id))["album_id"]

    # What recluster does when a stray keeps its declared album: a bare
    # album row is minted with no cover, the old row orphan-deleted.
    import time
    await db.insert_album({"id": "album_minted1", "title": "Bee Moved",
                           "artist_id": "unknown_artist",
                           "last_updated": int(time.time())})
    await db.reassign_album(track_id, "album_minted1")
    await db.delete_orphaned_albums_and_artists()
    assert (await db.get_album_by_id(old_album)) is None

    saved = await fi.backfill_embedded_art([str(music_dir)])
    assert saved == {"albums": 1, "tracks": 0}
    album = await db.get_album_by_id("album_minted1")
    assert album["image_url"] == "album_minted1.jpg"
    assert (fi.artwork_path / "album" / "album_minted1_large.jpg").exists()


@pytest.mark.asyncio
async def test_backfill_skips_artless_and_unavailable(indexer):
    fi, db, music_dir, config = indexer
    artless = music_dir / "artless.flac"
    _write_flac(artless, {"title": "Plain", "artist": "A"})
    changes = await fi.process_file(str(artless))
    await db.reassign_album(changes["tracks"], "unknown_album")

    # No art_phash in evidence -> not even selected for a file read.
    assert await db.get_singles_missing_art() == []

    covered = music_dir / "covered.flac"
    _write_flac(covered, {"title": "Covered", "artist": "A"},
                cover=_cover_png((5, 5, 5)))
    covered_changes = await fi.process_file(str(covered))
    covered_id = covered_changes["tracks"]
    # Strip the art it got at index time to expose the root-availability gate.
    await db.update_track(covered_id, {"image_url": None})

    # The track's root is not in the available list -> untouched.
    assert await fi.backfill_embedded_art([]) == {"albums": 0, "tracks": 0}
    track = await db.get_track_by_id(covered_id)
    assert track["image_url"] is None


@pytest.mark.asyncio
async def test_embedded_art_reads_mp3_apic(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "single.mp3"
    from mutagen.id3 import APIC, ID3

    id3 = ID3()
    id3.add(APIC(encoding=3, mime="image/png", type=3, desc="",
                 data=_cover_png((90, 10, 130))))
    id3.save(str(path))

    art = fi._embedded_art(LOCAL, str(path))
    assert art == _cover_png((90, 10, 130))
    with pytest.raises(OSError):
        fi._embedded_art(LOCAL, str(music_dir / "missing.mp3"))


@pytest.mark.asyncio
async def test_embedded_art_reads_flac_front_cover(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "single.flac"
    _write_flac(path, {"title": "T"}, cover=_cover_png((0, 200, 40)))
    assert fi._embedded_art(LOCAL, str(path)) == _cover_png((0, 200, 40))

    artless = music_dir / "plain.flac"
    _write_flac(artless, {"title": "P"})
    assert fi._embedded_art(LOCAL, str(artless)) is None


class _FakeDb:
    def __init__(self, track):
        self._track = track

    def is_good(self):
        return True

    def get_tracks_by_ids(self, track_ids):
        return [self._track] if self._track["id"] in track_ids else []

    def get_album_by_id(self, album_id):
        return None


@pytest.mark.asyncio
async def test_track_metadata_falls_back_to_track_art(tmp_path):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path / "music")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    track = {
        "id": "track_1",
        "album_id": "unknown_album",
        "artist_id": "artist_1",
        "album_title": "Unknown Album",
        "artist_name": "Blue Coast",
        "title": "Bee Moved",
        "duration": 100,
        "format": "audio/flac",
        "file_path": str(tmp_path / "music" / "bee.flac"),
        "image_url": "track_1.jpg",
    }
    module = LocalFilesInputModule(config, _FakeDb(track))
    track_dir = module.artwork_path / "track"
    track_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (50, 50), (1, 2, 3)).save(
        track_dir / "track_1_thumbnail.jpg", "JPEG"
    )

    [info] = await module.get_track_info(["track_1"])
    assert info.metadata.album.image is not None
    assert (
        info.metadata.album.image.thumbnail
        == "/resource/track/track_1_thumbnail.jpg"
    )
