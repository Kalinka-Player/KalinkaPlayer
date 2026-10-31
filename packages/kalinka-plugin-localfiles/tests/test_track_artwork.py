#!/usr/bin/env python3
"""A track keeps its own embedded cover where it differs from its album's.

The album has one cover for grids and headers; a track that embeds another
picture — a compilation of singles, a disc with its own sleeve — shows that
one in the queue and Now Playing. Tracks sharing a picture share one stored
copy, and tracks showing their album's cover store nothing.
"""

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

import kalinka_plugin_localfiles.indexer.artwork_resolver as resolver_module
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

from art_images import patterned_image, patterned_png


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


def _flac(path, track, cover, album="Murder Ballads"):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    audio.update(
        {"title": path.stem, "artist": "Nick Cave", "tracknumber": str(track)}
    )
    if album:
        audio["album"] = album
    if cover is not None:
        pic = Picture()
        pic.type = PictureType.COVER_FRONT
        pic.mime = "image/png"
        pic.data = cover
        audio.add_picture(pic)
    audio.save()
    return path


async def _ids(fi, *paths):
    return [(await fi.process_file(str(path)))["tracks"] for path in paths]


async def _image_urls(db, ids):
    return [(await db.get_track_by_id(tid))["image_url"] for tid in ids]


def _track_stores(monkeypatch):
    stores = []
    real = resolver_module.store_artwork_images

    def store(path, data, entity_id, entity_type, origin=None):
        if entity_type == "track":
            stores.append(entity_id)
        return real(path, data, entity_id, entity_type, origin)

    monkeypatch.setattr(resolver_module, "store_artwork_images", store)
    return stores


@pytest.mark.asyncio
async def test_a_track_with_another_cover_keeps_it_and_the_album_keeps_its_own(
    indexer,
):
    fi, db, music_dir, config = indexer
    folder = music_dir / "Murder Ballads"
    folder.mkdir()
    patterned_image(1, 500).save(folder / "cover.jpg")
    ids = await _ids(
        fi,
        _flac(folder / "01.flac", 1, patterned_png(1)),
        _flac(folder / "02.flac", 2, patterned_png(1)),
        _flac(folder / "03.flac", 3, patterned_png(2)),
    )

    await fi.resolve_artwork([str(music_dir)])

    assert await _image_urls(db, ids) == [None, None, f"{ids[2]}.jpg"]

    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    infos = await module.get_track_info(ids)
    album_id = (await db.get_track_by_id(ids[0]))["album_id"]
    album_cover = f"/resource/album/{album_id}_small.jpg"
    assert [info.metadata.album.image.small for info in infos] == [album_cover] * 3
    assert [info.metadata.image.small for info in infos] == [
        album_cover,
        album_cover,
        f"/resource/track/{ids[2]}_small.jpg",
    ]


@pytest.mark.asyncio
async def test_tracks_sharing_a_cover_share_one_stored_copy(indexer, monkeypatch):
    fi, db, music_dir, config = indexer
    folder = music_dir / "Murder Ballads"
    ids = await _ids(
        fi,
        *(
            _flac(folder / f"0{n}.flac", n, patterned_png(1 if n <= 4 else 2))
            for n in range(1, 8)
        ),
    )
    stores = _track_stores(monkeypatch)

    await fi.resolve_artwork([str(music_dir)])

    assert stores == [ids[4]]
    assert await _image_urls(db, ids) == [None] * 4 + [f"{ids[4]}.jpg"] * 3

    await fi.resolve_artwork([str(music_dir)])
    assert stores == [ids[4]]


@pytest.mark.asyncio
async def test_a_track_without_art_shows_its_albums_cover(indexer):
    fi, db, music_dir, config = indexer
    folder = music_dir / "Murder Ballads"
    ids = await _ids(
        fi,
        _flac(folder / "01.flac", 1, patterned_png(1)),
        _flac(folder / "02.flac", 2, None),
    )
    await fi.resolve_artwork([str(music_dir)])

    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    [info] = await module.get_track_info([ids[1]])

    assert info.metadata.image == info.metadata.album.image
    assert info.metadata.image is not None


@pytest.mark.asyncio
async def test_a_single_that_joins_an_album_with_its_cover_lets_its_own_go(
    indexer,
):
    fi, db, music_dir, config = indexer
    folder = music_dir / "Murder Ballads"
    album_track, single = await _ids(
        fi,
        _flac(folder / "01.flac", 1, patterned_png(1)),
        _flac(folder / "02.flac", 2, patterned_png(1), album=None),
    )
    await fi.resolve_artwork([str(music_dir)])
    assert await _image_urls(db, [single]) == [f"{single}.jpg"]

    album_id = (await db.get_track_by_id(album_track))["album_id"]
    await db.reassign_album(single, album_id)
    await fi.resolve_artwork([str(music_dir)])

    assert await _image_urls(db, [single]) == [None]


@pytest.mark.asyncio
async def test_a_single_keeps_its_cover_in_the_album_field_for_older_clients(
    indexer,
):
    fi, db, music_dir, config = indexer
    (single,) = await _ids(fi, _flac(music_dir / "loose.flac", 1, patterned_png(1), None))
    await fi.resolve_artwork([str(music_dir)])

    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    [info] = await module.get_track_info([single])

    own = f"/resource/track/{single}_small.jpg"
    assert info.metadata.image.small == own
    assert info.metadata.album.image.small == own


@pytest.mark.asyncio
async def test_loose_tracks_are_covered_root_by_root(tmp_path):
    """Loose tracks share one pseudo-album across every music folder; one
    folder being away must not hold up the others'."""
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(root) for root in roots],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    db = AsyncIndexerDb(config)
    fi = FileIndexer(config, db)
    away, here = await _ids(
        fi,
        _flac(roots[0] / "loose.flac", 1, patterned_png(1), album=None),
        _flac(roots[1] / "loose.flac", 1, patterned_png(2), album=None),
    )

    await fi.resolve_artwork([str(roots[1])])

    assert await _image_urls(db, [away, here]) == [None, f"{here}.jpg"]
