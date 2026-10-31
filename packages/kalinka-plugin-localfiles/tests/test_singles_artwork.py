#!/usr/bin/env python3
"""Singles on unknown_album have no album row to carry their embedded cover,
so they get track-level art: saved at index time when the track lands there
directly, and backfilled from the file after clustering demotes it and drops
its per-file album row.
"""

import errno
import io
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.utils.artwork_store import dhash_bytes

from art_images import patterned_png


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
LADDER_MP3 = (
    Path(__file__).resolve().parents[2]
    / "kalinka-renderer"
    / "tests"
    / "data"
    / "ladder.mp3"
)


def _cover_png(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


TAGS = {"title": "Bee Moved", "artist": "Blue Coast"}


def _write_flac(path, tags, cover=None, *, pictures=()):
    """``cover`` is embedded as the front, then ``pictures`` as (type, bytes)."""
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    front = [] if cover is None else [(PictureType.COVER_FRONT, cover)]
    for kind, data in front + list(pictures):
        pic = Picture()
        pic.type = kind
        pic.mime = "image/png"
        pic.data = data
        audio.add_picture(pic)
    audio.save()


def _track_reads(monkeypatch, fi, junk=()):
    """Records each embedded-cover read the cover pass makes, answering
    ``junk`` paths with a picture that will not decode."""
    reads = []
    real = fi.artwork_resolver.read_cover

    def read(storage, path):
        reads.append(path)
        return b"junk" if path in junk else real(storage, path)

    monkeypatch.setattr(fi.artwork_resolver, "read_cover", read)
    return reads


async def _single(fi, music_dir, cover=None):
    path = music_dir / "loose.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast"}, cover=cover)
    track_id = (await fi.process_file(str(path)))["tracks"]
    return path, track_id


@pytest.mark.asyncio
async def test_an_untagged_single_keeps_its_own_cover(indexer):
    fi, db, music_dir, config = indexer
    path, track_id = await _single(fi, music_dir, patterned_png(1))
    assert (await db.get_track_by_id(track_id))["album_id"] == "unknown_album"

    await fi.resolve_artwork([str(music_dir)])

    assert (await db.get_track_by_id(track_id))["image_url"] == f"{track_id}.jpg"
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
async def test_a_single_retagged_without_art_drops_its_cover(indexer):
    fi, db, music_dir, config = indexer
    path, track_id = await _single(fi, music_dir, patterned_png(1))
    await fi.resolve_artwork([str(music_dir)])

    path.unlink()
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast"})
    await fi.process_file(str(path))
    await fi.resolve_artwork([str(music_dir)])

    assert (await db.get_track_by_id(track_id))["image_url"] is None


@pytest.mark.asyncio
async def test_a_track_demoted_to_a_single_gets_its_cover_back(indexer, monkeypatch):
    fi, db, music_dir, config = indexer
    path = music_dir / "single.flac"
    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast",
                       "album": "Bee Moved"},
                cover=patterned_png(1))
    track_id = (await fi.process_file(str(path)))["tracks"]
    await fi.resolve_artwork([str(music_dir)])
    assert (await db.get_track_by_id(track_id))["image_url"] is None

    # What recluster does to a V/A dump folder: the track goes to
    # unknown_album and the per-file album row is orphan-deleted.
    await db.reassign_album(track_id, "unknown_album")
    await db.delete_orphaned_albums_and_artists()
    await fi.resolve_artwork([str(music_dir)])

    assert (await db.get_track_by_id(track_id))["image_url"] == f"{track_id}.jpg"
    reads = _track_reads(monkeypatch, fi)
    await fi.resolve_artwork([str(music_dir)])
    assert reads == []


@pytest.mark.asyncio
async def test_a_cover_that_will_not_decode_is_not_read_again_until_it_changes(
    indexer, monkeypatch
):
    fi, db, music_dir, config = indexer
    path, track_id = await _single(fi, music_dir, patterned_png(1))
    reads = _track_reads(monkeypatch, fi, junk={str(path)})

    await fi.resolve_artwork([str(music_dir)])
    await fi.resolve_artwork([str(music_dir)])

    assert reads == [str(path)]
    assert (await db.get_track_by_id(track_id))["image_url"] is None

    _write_flac(path, {"title": "Bee Moved", "artist": "Blue Coast"},
                cover=patterned_png(2))
    os.utime(path, (1, 2_000_000_000))
    await fi.process_file(str(path))
    monkeypatch.undo()
    reads = _track_reads(monkeypatch, fi)
    await fi.resolve_artwork([str(music_dir)])

    assert reads == [str(path)]
    assert (await db.get_track_by_id(track_id))["image_url"] == f"{track_id}.jpg"


@pytest.mark.asyncio
async def test_a_file_that_could_not_be_read_is_tried_again(indexer, monkeypatch):
    fi, db, music_dir, config = indexer
    path, track_id = await _single(fi, music_dir, patterned_png(1))
    real = fi.artwork_resolver.read_cover

    def unreachable(storage, file_path):
        raise OSError("host is down")

    monkeypatch.setattr(fi.artwork_resolver, "read_cover", unreachable)
    await fi.resolve_artwork([str(music_dir)])
    assert (await db.get_track_by_id(track_id))["image_url"] is None

    monkeypatch.setattr(fi.artwork_resolver, "read_cover", real)
    await fi.resolve_artwork([str(music_dir)])
    assert (await db.get_track_by_id(track_id))["image_url"] == f"{track_id}.jpg"


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

    assert await fi.resolve_artwork([str(music_dir)]) == 1
    album = await db.get_album_by_id("album_minted1")
    assert album["image_url"] == "album_minted1.jpg"
    assert (fi.artwork_path / "album" / "album_minted1_large.jpg").exists()


@pytest.mark.asyncio
async def test_an_artless_single_or_one_out_of_reach_is_left_alone(indexer):
    fi, db, music_dir, config = indexer
    _, artless = await _single(fi, music_dir)
    covered = music_dir / "covered.flac"
    _write_flac(covered, {"title": "Covered", "artist": "A"}, cover=patterned_png(1))
    covered_id = (await fi.process_file(str(covered)))["tracks"]

    await fi.resolve_artwork([])
    assert (await db.get_track_by_id(covered_id))["image_url"] is None

    await fi.resolve_artwork([str(music_dir)])
    assert (await db.get_track_by_id(artless))["image_url"] is None
    assert (await db.get_track_by_id(covered_id))["image_url"] == f"{covered_id}.jpg"


@pytest.mark.asyncio
async def test_embedded_art_reads_mp3_apic(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "single.mp3"
    shutil.copyfile(LADDER_MP3, path)
    from mutagen.id3 import APIC, ID3

    id3 = ID3()
    id3.add(APIC(encoding=3, mime="image/png", type=3, desc="",
                 data=_cover_png((90, 10, 130))))
    id3.save(str(path))

    art = fi._embedded_art(LOCAL, str(path))
    assert art == _cover_png((90, 10, 130))
    with pytest.raises(OSError):
        fi._embedded_art(LOCAL, str(music_dir / "missing.mp3"))


def _mp3_with_pictures(music_dir, *pictures):
    """A copy of the ladder MP3 tagged with ``(type, desc, data)`` APICs."""
    from mutagen.id3 import APIC, ID3

    path = music_dir / "pictures.mp3"
    shutil.copyfile(LADDER_MP3, path)
    id3 = ID3()
    for kind, desc, data in pictures:
        id3.add(APIC(encoding=3, mime="image/png", type=kind, desc=desc, data=data))
    id3.save(str(path))
    return path


@pytest.mark.asyncio
async def test_an_mp3_front_cover_is_found_by_its_type_not_its_description(
    indexer,
):
    """Taggers describe frames freely: "Front Cover" is the front, and an
    undescribed frame may be the back."""
    fi, db, music_dir, config = indexer
    front, back = _cover_png((90, 10, 130)), _cover_png((10, 90, 130))
    path = _mp3_with_pictures(
        music_dir,
        (PictureType.COVER_BACK, "", back),
        (PictureType.COVER_FRONT, "Front Cover", front),
    )

    assert fi._embedded_cover(LOCAL, str(path)) == (front, 3)


@pytest.mark.asyncio
async def test_an_mp3_with_only_a_back_cover_has_none(indexer):
    fi, db, music_dir, config = indexer
    path = _mp3_with_pictures(
        music_dir, (PictureType.COVER_BACK, "", _cover_png((10, 90, 130)))
    )

    assert fi._embedded_cover(LOCAL, str(path)) is None
    assert "album_art" not in fi._extract_metadata(LOCAL, str(path))


@pytest.mark.asyncio
async def test_an_untyped_picture_stands_in_and_says_so(indexer):
    fi, db, music_dir, config = indexer
    other = _cover_png((40, 40, 200))
    path = _mp3_with_pictures(
        music_dir,
        (PictureType.ARTIST, "", _cover_png((200, 40, 40))),
        (PictureType.OTHER, "", other),
    )

    metadata = fi._extract_metadata(LOCAL, str(path))
    assert metadata["album_art"] == other
    assert metadata["album_art_type"] == 0


@pytest.mark.asyncio
async def test_covers_picked_by_older_readers_are_read_again_once(indexer):
    """An MP3 indexed when an undescribed back cover was taken for the
    front gets its evidence corrected on the next scan, and only that one."""
    fi, db, music_dir, config = indexer
    front, back = patterned_png(1), patterned_png(2)
    path = _mp3_with_pictures(
        music_dir,
        (PictureType.COVER_BACK, "", back),
        (PictureType.COVER_FRONT, "Front Cover", front),
    )
    track_id = (await fi.process_file(str(path)))["tracks"]
    with closing(sqlite3.connect(config.db_path)) as conn:
        conn.execute(
            "UPDATE track_evidence SET art_phash = ?, art_type = NULL, "
            "updated_at = 0",
            (dhash_bytes(back),),
        )
        conn.commit()

    assert await fi._reread_covers_of_older_readers([]) == 0
    assert await db.get_indexer_state("cover_readers") != "1:done"
    assert await fi._reread_covers_of_older_readers([str(music_dir)]) == 1
    assert await db.get_indexer_state("cover_readers") == "1:done"
    assert await fi._reread_covers_of_older_readers([str(music_dir)]) == 0

    with closing(sqlite3.connect(config.db_path)) as conn:
        row = conn.execute(
            "SELECT art_phash, art_type FROM track_evidence WHERE track_id = ?",
            (track_id,),
        ).fetchone()
    assert row == (dhash_bytes(front), 3)


@pytest.mark.asyncio
async def test_a_flac_picture_records_its_type_with_its_hash(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "other.flac"
    _write_flac(path, TAGS, cover=_cover_png((20, 90, 250)))
    audio = FLAC(str(path))
    audio.pictures[0].type = PictureType.OTHER
    pictures = list(audio.pictures)
    audio.clear_pictures()
    for picture in pictures:
        audio.add_picture(picture)
    audio.save()

    await fi.process_file(str(path))

    (_, evidence), = await db.get_tracks_with_evidence()
    with closing(sqlite3.connect(config.db_path)) as conn:
        (art_type,) = conn.execute("SELECT art_type FROM track_evidence").fetchone()
    assert evidence["art_phash"] is not None
    assert art_type == 0


@pytest.mark.asyncio
async def test_a_stub_too_short_for_an_id3v1_tag_is_broken_not_unreadable(indexer):
    """mutagen looks for ID3v1 by seeking 131 bytes back from the end, and
    handles the refusal a shorter file gets; that is not a failed read."""
    fi, db, music_dir, config = indexer
    path = music_dir / "stub.mp3"
    path.write_bytes(b"not an mp3 at all")

    assert fi._embedded_art(LOCAL, str(path)) is None


class _InvalidReadFile(io.BytesIO):
    def read(self, size=-1):
        raise OSError(errno.EINVAL, "Invalid argument")


@pytest.mark.asyncio
async def test_a_read_refused_as_invalid_is_still_unreadable(indexer, monkeypatch):
    """Only a seek's EINVAL is the parser probing; a read's is the storage."""
    fi, db, music_dir, config = indexer
    path = music_dir / "single.mp3"
    path.write_bytes(b"\0" * 1024)
    monkeypatch.setattr(
        LocalStorage,
        "open",
        lambda self, p, *, read_ahead_bytes=None: _InvalidReadFile(),
    )

    with pytest.raises(OSError):
        fi._embedded_art(LOCAL, str(path))


@pytest.mark.asyncio
async def test_embedded_art_reads_flac_front_cover(indexer):
    fi, db, music_dir, config = indexer
    path = music_dir / "single.flac"
    _write_flac(path, {"title": "T"}, cover=_cover_png((0, 200, 40)))
    assert fi._embedded_art(LOCAL, str(path)) == _cover_png((0, 200, 40))

    artless = music_dir / "plain.flac"
    _write_flac(artless, {"title": "P"})
    assert fi._embedded_art(LOCAL, str(artless)) is None


@pytest.mark.parametrize(
    "pictures, cover",
    [
        (
            [
                (PictureType.LEAFLET_PAGE, b"lyrics"),
                (PictureType.COVER_FRONT, b"front"),
            ],
            b"front",
        ),
        (
            [(PictureType.OTHER, b"other"), (PictureType.COVER_FRONT, b"front")],
            b"front",
        ),
        (
            [(PictureType.LEAFLET_PAGE, b"lyrics"), (PictureType.OTHER, b"other")],
            b"other",
        ),
        (
            [
                (PictureType.LEAFLET_PAGE, b"lyrics"),
                (PictureType.COVER_BACK, b"back"),
                (PictureType.MEDIA, b"label"),
            ],
            None,
        ),
    ],
    ids=["front-listed-last", "front-over-other", "other-without-front", "no-front"],
)
@pytest.mark.asyncio
async def test_only_a_front_or_untyped_picture_is_the_cover(indexer, pictures, cover):
    """A rip that embeds its booklet must not show a lyrics page as its cover."""
    fi, db, music_dir, config = indexer
    path = music_dir / "booklet.flac"
    _write_flac(path, {"title": "T"}, pictures=pictures)

    assert fi._extract_metadata(LOCAL, str(path)).get("album_art") == cover
    assert fi._embedded_art(LOCAL, str(path)) == cover


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
