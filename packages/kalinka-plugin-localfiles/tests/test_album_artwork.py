#!/usr/bin/env python3
"""Which cover an album gets, from its folder and the art its files embed.

An image a folder belonging to the album names as its cover wins; then the
embedded front cover most tracks carry; then an image picked by its look. A
folder shared by several albums belongs to none of them. Covers follow the
library as it changes, and a pass over an unchanged album opens nothing.
"""

import json
import os
import sqlite3
from contextlib import closing

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer, _worth_indexing
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.storage import ChangeKind
from kalinka_plugin_localfiles.storage.local import LocalStorage
from kalinka_plugin_localfiles.utils.artwork_store import (
    dhash_bytes,
    dhash_image,
    save_artwork_images,
)

from art_images import patterned_image, patterned_png

FRONT, OTHER = PictureType.COVER_FRONT, PictureType.OTHER


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


def _flac(path, album, track=1, cover=None, kind=FRONT, disc=None, title=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    audio["title"] = title or path.stem
    audio["artist"] = "Nick Cave"
    if album is not None:
        audio["album"] = album
    audio["tracknumber"] = str(track)
    if disc is not None:
        audio["discnumber"] = str(disc)
    if cover is not None:
        pic = Picture()
        pic.type = kind
        pic.mime = "image/png"
        pic.data = cover
        audio.add_picture(pic)
    audio.save()
    return path


def _image(path, seed, size=500):
    path.parent.mkdir(parents=True, exist_ok=True)
    patterned_image(seed, size).save(path)
    return path


async def _index(fi, *paths):
    album_ids = []
    for path in paths:
        changes = await fi.process_file(str(path))
        track = await fi.db_manager.get_track_by_id(changes["tracks"])
        album_ids.append(track["album_id"])
    return album_ids


def _stored_seed(fi, album_id, seeds):
    """Which of ``seeds`` the album's stored cover shows."""
    stored = dhash_bytes(
        (fi.artwork_path / "album" / f"{album_id}_small.jpg").read_bytes()
    )
    distance = {
        seed: bin(int(stored, 16) ^ int(dhash_image(patterned_image(seed)), 16)).count("1")
        for seed in seeds
    }
    best = min(distance, key=distance.get)
    assert distance[best] <= 8, distance
    return best


def _origin(config, album_id):
    with closing(sqlite3.connect(config.db_path)) as conn:
        row = conn.execute(
            "SELECT evidence_ref FROM resolved_origin WHERE entity_type = 'album' "
            "AND entity_id = ? AND field = 'image_url'",
            (album_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _forget_first_covers(config):
    """As if the album rows were minted by clustering, coverless."""
    with closing(sqlite3.connect(config.db_path)) as conn:
        conn.execute("UPDATE albums SET image_url = NULL")
        conn.execute("DELETE FROM resolved_origin WHERE field = 'image_url'")
        conn.commit()


class _Opens:
    """Counts the files the storage opens."""

    def __init__(self, monkeypatch):
        self.paths = []
        real = LocalStorage.open

        def open_(storage, path, *, read_ahead_bytes=None):
            self.paths.append(path)
            return real(storage, path, read_ahead_bytes=read_ahead_bytes)

        monkeypatch.setattr(LocalStorage, "open", open_)


class TestPrecedence:
    @pytest.mark.asyncio
    async def test_a_sleeve_beside_the_audio_becomes_the_cover(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        _image(folder / "cover.jpg", 1)

        assert await fi.resolve_artwork([str(music_dir)]) == 1

        row = await db.get_album_by_id(album)
        assert (row["image_url"], row["image_generated"]) == (f"{album}.jpg", 0)
        assert _stored_seed(fi, album, [1]) == 1
        origin = _origin(config, album)
        assert origin["source"] == "folder_art"
        assert origin["path"] == str(folder / "cover.jpg")
        assert origin["reason"] == "album folder cover"

    @pytest.mark.asyncio
    async def test_the_folder_cover_beats_the_embedded_one(self, indexer):
        """cover.jpg is its owner's say-so for the whole album."""
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", cover=patterned_png(2))
        )
        _image(folder / "cover.jpg", 1)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [1, 2]) == 1

    @pytest.mark.asyncio
    async def test_embedded_art_beats_a_scan_named_nothing_in_particular(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", cover=patterned_png(2))
        )
        _image(folder / "PIC" / "abbey_1.jpg", 1, size=1500)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [1, 2]) == 2
        assert _origin(config, album)["source"] == "embedded_art"

    @pytest.mark.asyncio
    async def test_a_scan_is_taken_when_nothing_else_is_there(self, indexer):
        """A needledrop's sleeve scan, in a scans folder, unnamed."""
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        _image(folder / "PIC" / "abbey_1.jpg", 1, size=1500)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [1]) == 1
        assert _origin(config, album)["reason"] == "album folder scan"

    @pytest.mark.asyncio
    async def test_a_booklet_page_inside_the_file_gives_way_to_the_sleeve(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi,
            _flac(
                folder / "01.flac",
                "Murder Ballads",
                cover=patterned_png(2),
                kind=PictureType.LEAFLET_PAGE,
            ),
        )
        _image(folder / "cover.jpg", 1)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [1]) == 1

    @pytest.mark.asyncio
    async def test_the_cover_most_tracks_carry_wins(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        albums = await _index(
            fi,
            _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(5)),
            _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4)),
            _flac(folder / "03.flac", "Murder Ballads", 3, cover=patterned_png(4)),
        )
        album = albums[0]

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [4, 5]) == 4
        origin = _origin(config, album)
        assert origin["reason"] == "most common embedded front cover"
        assert origin["phash"] == dhash_bytes(patterned_png(4))

    @pytest.mark.asyncio
    async def test_a_tie_goes_to_the_earliest_track_whatever_the_scan_order(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        second = _flac(folder / "a.flac", "Murder Ballads", 2, cover=patterned_png(6))
        first = _flac(folder / "b.flac", "Murder Ballads", 1, cover=patterned_png(7))
        album, _ = await _index(fi, second, first)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [6, 7]) == 7

    @pytest.mark.asyncio
    async def test_a_front_cover_beats_a_more_common_untyped_picture(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        albums = await _index(
            fi,
            _flac(folder / "01.flac", "Murder Ballads", 1, patterned_png(8), OTHER),
            _flac(folder / "02.flac", "Murder Ballads", 2, patterned_png(8), OTHER),
            _flac(folder / "03.flac", "Murder Ballads", 3, patterned_png(9), FRONT),
        )

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, albums[0], [8, 9]) == 9

    @pytest.mark.asyncio
    async def test_an_untyped_picture_serves_when_no_track_has_a_front(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", 1, patterned_png(8), OTHER)
        )
        _forget_first_covers(config)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [8]) == 8
        assert _origin(config, album)["reason"] == (
            "most common untyped embedded picture"
        )

    @pytest.mark.asyncio
    async def test_an_album_without_local_art_is_left_to_the_online_sources(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        (album,) = await _index(fi, _flac(music_dir / "MB" / "01.flac", "Murder Ballads"))

        assert await fi.resolve_artwork([str(music_dir)]) == 0

        assert (await db.get_album_by_id(album))["image_url"] is None
        assert _origin(config, album)["source"] == "none"

    @pytest.mark.asyncio
    async def test_local_art_replaces_a_generated_placeholder(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        await fi.resolve_artwork([str(music_dir)])
        await db.update_album(album, {"image_url": album, "image_generated": 1})
        _image(folder / "cover.jpg", 1)

        await fi.resolve_artwork([str(music_dir)], dirty_dirs={str(folder)})

        assert (await db.get_album_by_id(album))["image_generated"] == 0
        assert _stored_seed(fi, album, [1]) == 1

    @pytest.mark.asyncio
    async def test_local_art_replaces_an_online_cover(self, indexer):
        """A cover.jpg that arrives after Deezer answered still wins."""
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        await fi.resolve_artwork([str(music_dir)])
        save_artwork_images(fi.artwork_path, patterned_png(3), album, "album")
        await db.update_album(album, {"image_url": album, "image_generated": 0})
        _image(folder / "cover.jpg", 1)

        await fi.resolve_artwork([str(music_dir)], dirty_dirs={str(folder)})

        assert _stored_seed(fi, album, [1, 3]) == 1

    @pytest.mark.asyncio
    async def test_a_folded_inlay_is_stored_as_its_front_panel(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        spread = Image.new("RGB", (3110, 1692), (10, 10, 200))
        spread.paste(Image.new("RGB", (1555, 1692), (200, 10, 10)), (1555, 0))
        spread.save(folder / "cover.jpg")

        await fi.resolve_artwork([str(music_dir)])

        with Image.open(fi.artwork_path / "album" / f"{album}_large.jpg") as saved:
            assert saved.width / saved.height < 1.2
            red, _, blue = saved.convert("RGB").getpixel(
                (saved.width // 2, saved.height // 2)
            )
        assert red > 150 and blue < 100


class TestFolderOwnership:
    @pytest.mark.asyncio
    async def test_single_file_albums_sharing_a_folder_keep_their_own_covers(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        mixed = music_dir / "Mixed"
        a, b = await _index(
            fi,
            _flac(mixed / "Album A.flac", "Album A", cover=patterned_png(11)),
            _flac(mixed / "Album B.flac", "Album B", cover=patterned_png(12)),
        )
        _image(mixed / "cover.jpg", 13)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, a, [11, 12, 13]) == 11
        assert _stored_seed(fi, b, [11, 12, 13]) == 12

    @pytest.mark.asyncio
    async def test_a_shared_folder_cover_goes_to_none_of_its_albums(self, indexer):
        fi, db, music_dir, config = indexer
        mixed = music_dir / "Mixed"
        a, b = await _index(
            fi, _flac(mixed / "A.flac", "Album A"), _flac(mixed / "B.flac", "Album B")
        )
        _image(mixed / "cover.jpg", 13)

        await fi.resolve_artwork([str(music_dir)])

        assert (await db.get_album_by_id(a))["image_url"] is None
        assert (await db.get_album_by_id(b))["image_url"] is None

    @pytest.mark.asyncio
    async def test_a_loose_track_beside_the_album_keeps_the_folder_nobodys(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        album, loose = await _index(
            fi,
            _flac(folder / "01.flac", "Murder Ballads"),
            _flac(folder / "stray.flac", None),
        )
        assert loose == "unknown_album"
        _image(folder / "cover.jpg", 1)

        await fi.resolve_artwork([str(music_dir)])

        assert (await db.get_album_by_id(album))["image_url"] is None

    @pytest.mark.asyncio
    async def test_the_library_root_belongs_to_no_album(self, indexer):
        fi, db, music_dir, config = indexer
        (album,) = await _index(fi, _flac(music_dir / "01.flac", "Murder Ballads"))
        _image(music_dir / "folder.jpg", 1)

        await fi.resolve_artwork([str(music_dir)])

        assert (await db.get_album_by_id(album))["image_url"] is None

    @pytest.mark.asyncio
    async def test_discs_in_subfolders_share_the_cover_above_them(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        one, two = await _index(
            fi,
            _flac(folder / "CD1" / "01.flac", "Murder Ballads", 1, disc=1),
            _flac(folder / "CD2" / "01.flac", "Murder Ballads", 1, disc=2),
        )
        assert one == two
        _image(folder / "cover.jpg", 1)
        _image(folder / "CD2" / "cover.jpg", 2)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, one, [1, 2]) == 1

    @pytest.mark.asyncio
    async def test_a_cover_kept_only_in_a_disc_folder_is_found(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        album, _ = await _index(
            fi,
            _flac(folder / "CD1" / "01.flac", "Murder Ballads", 1, disc=1),
            _flac(folder / "CD2" / "01.flac", "Murder Ballads", 1, disc=2),
        )
        _image(folder / "CD1" / "folder.jpg", 1)

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [1]) == 1

    @pytest.mark.asyncio
    async def test_loose_tracks_do_not_take_a_neighbouring_albums_cover(
        self, indexer
    ):
        """An artist folder holding loose tracks and, a level down, an
        album folder: the album's cover is its own, not the loose tracks'."""
        fi, db, music_dir, config = indexer
        artist = music_dir / "Nick Cave"
        loose, album = await _index(
            fi,
            _flac(artist / "b-side.flac", "B-Sides"),
            _flac(artist / "Murder Ballads" / "CD1" / "01.flac", "Murder Ballads"),
        )
        _image(artist / "Murder Ballads" / "cover.jpg", 1)

        await fi.resolve_artwork([str(music_dir)])

        assert (await db.get_album_by_id(loose))["image_url"] is None
        assert _stored_seed(fi, album, [1]) == 1


class TestChanges:
    async def _embedded_album(self, fi, music_dir):
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", cover=patterned_png(2))
        )
        await fi.resolve_artwork([str(music_dir)])
        return folder, album

    @pytest.mark.asyncio
    async def test_a_cover_added_later_is_taken_where_the_change_was_seen(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder, album = await self._embedded_album(fi, music_dir)
        _image(folder / "cover.jpg", 1)

        assert await fi.resolve_artwork([str(music_dir)]) == 0
        assert await fi.resolve_artwork(
            [str(music_dir)], dirty_dirs={str(folder)}
        ) == 1
        assert _stored_seed(fi, album, [1, 2]) == 1

    @pytest.mark.asyncio
    async def test_a_cover_added_later_is_found_by_the_next_full_scan(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder, album = await self._embedded_album(fi, music_dir)
        await fi.run_scan()
        _image(folder / "cover.jpg", 1)

        await fi.run_scan()

        assert _stored_seed(fi, album, [1, 2]) == 1

    @pytest.mark.asyncio
    async def test_a_watched_cover_reaches_the_album(self, indexer):
        fi, db, music_dir, config = indexer
        folder, album = await self._embedded_album(fi, music_dir)
        cover = _image(folder / "cover.jpg", 1)

        assert _worth_indexing(ChangeKind.FILE_WRITTEN, str(cover))
        await fi.handle_incremental_changes({(ChangeKind.FILE_WRITTEN, str(cover))})

        assert _stored_seed(fi, album, [1, 2]) == 1

    @pytest.mark.asyncio
    async def test_a_removed_cover_gives_way_to_the_embedded_one(self, indexer):
        fi, db, music_dir, config = indexer
        folder, album = await self._embedded_album(fi, music_dir)
        cover = _image(folder / "cover.jpg", 1)
        await fi.resolve_artwork([str(music_dir)], dirty_dirs={str(folder)})
        cover.unlink()

        await fi.resolve_artwork([str(music_dir)], dirty_dirs={str(folder)})

        assert _stored_seed(fi, album, [1, 2]) == 2

    @pytest.mark.asyncio
    async def test_a_cover_replaced_in_place_is_taken_again(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        cover = _image(folder / "cover.jpg", 1)
        await fi.resolve_artwork([str(music_dir)])
        _image(cover, 3, size=600)

        await fi.run_scan()

        assert _stored_seed(fi, album, [1, 3]) == 3

    @pytest.mark.asyncio
    async def test_the_cover_follows_the_tracks_majority(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        paths = [
            _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(4)),
            _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4)),
            _flac(folder / "03.flac", "Murder Ballads", 3, cover=patterned_png(5)),
        ]
        album, *_ = await _index(fi, *paths)
        await fi.resolve_artwork([str(music_dir)])
        _flac(paths[1], "Murder Ballads", 2, cover=patterned_png(5))
        os.utime(paths[1], (1, 2_000_000_000))
        await fi.process_file(str(paths[1]))

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [4, 5]) == 5

    @pytest.mark.asyncio
    async def test_a_track_moving_albums_moves_the_vote(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        moving = _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4))
        album, *_ = await _index(
            fi,
            _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(5)),
            moving,
            _flac(folder / "03.flac", "Murder Ballads", 3, cover=patterned_png(4)),
        )
        await fi.resolve_artwork([str(music_dir)])
        assert _stored_seed(fi, album, [4, 5]) == 4
        (track,) = [
            t for t in await db.get_all_tracks() if t["file_path"] == str(moving)
        ]
        await db.reassign_album(track["id"], "unknown_album")

        await fi.resolve_artwork([str(music_dir)])

        assert _stored_seed(fi, album, [4, 5]) == 5

    @pytest.mark.asyncio
    async def test_a_deleted_cache_file_is_stored_again(self, indexer):
        fi, db, music_dir, config = indexer
        folder, album = await self._embedded_album(fi, music_dir)
        for size in ("thumbnail", "small", "large"):
            (fi.artwork_path / "album" / f"{album}_{size}.jpg").unlink()

        assert await fi.resolve_artwork([str(music_dir)]) == 1
        assert _stored_seed(fi, album, [2]) == 2

    @pytest.mark.asyncio
    async def test_an_album_that_loses_its_local_art_is_reopened_online(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        cover = _image(folder / "cover.jpg", 1)
        await fi.resolve_artwork([str(music_dir)])
        await db.update_album(album, {"enriched": 1})
        cover.unlink()

        assert await fi.resolve_artwork(
            [str(music_dir)], dirty_dirs={str(folder)}
        ) == 1

        row = await db.get_album_by_id(album)
        assert (row["image_url"], row["enriched"]) == (None, 0)
        assert _origin(config, album)["source"] == "none"

    @pytest.mark.asyncio
    async def test_an_unavailable_root_is_not_touched(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        _image(folder / "cover.jpg", 1)

        assert await fi.resolve_artwork([]) == 0
        assert (await db.get_album_by_id(album))["image_url"] is None


class TestCost:
    @pytest.mark.asyncio
    async def test_a_second_pass_opens_nothing(self, indexer, monkeypatch):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        await _index(
            fi,
            _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(4)),
            _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4)),
        )
        _image(folder / "PIC" / "abbey_1.jpg", 1, size=1500)
        await fi.resolve_artwork([str(music_dir)])
        opens = _Opens(monkeypatch)

        assert await fi.resolve_artwork([str(music_dir)]) == 0
        assert opens.paths == []

    @pytest.mark.asyncio
    async def test_the_first_file_cover_is_kept_unopened_when_the_tracks_agree(
        self, indexer, monkeypatch
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        await _index(
            fi,
            _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(4)),
            _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4)),
        )
        opens = _Opens(monkeypatch)

        assert await fi.resolve_artwork([str(music_dir)]) == 0
        assert not [path for path in opens.paths if path.endswith(".flac")]

    @pytest.mark.asyncio
    async def test_a_cover_from_before_decisions_were_kept_is_adopted(
        self, indexer, monkeypatch
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", cover=patterned_png(4))
        )
        with closing(sqlite3.connect(config.db_path)) as conn:
            conn.execute("DELETE FROM resolved_origin WHERE field = 'image_url'")
            conn.commit()
        opens = _Opens(monkeypatch)

        assert await fi.resolve_artwork([str(music_dir)]) == 0
        assert not [path for path in opens.paths if path.endswith(".flac")]
        assert _origin(config, album)["source"] == "embedded_art"

    @pytest.mark.asyncio
    async def test_a_track_whose_cover_will_not_decode_gives_way_to_the_next(
        self, indexer, monkeypatch
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        first = _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(4))
        album, _ = await _index(
            fi,
            first,
            _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4)),
        )
        _forget_first_covers(config)
        real = fi.artwork_resolver.read_cover
        monkeypatch.setattr(
            fi.artwork_resolver,
            "read_cover",
            lambda storage, path: b"junk" if path == str(first) else real(storage, path),
        )

        assert await fi.resolve_artwork([str(music_dir)]) == 1
        assert _origin(config, album)["path"] == str(folder / "02.flac")

    @pytest.mark.asyncio
    async def test_a_read_that_fails_is_tried_again_next_pass(
        self, indexer, monkeypatch
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", cover=patterned_png(4))
        )
        _forget_first_covers(config)

        def unreachable(storage, path):
            raise OSError("host is down")

        real = fi.artwork_resolver.read_cover
        monkeypatch.setattr(fi.artwork_resolver, "read_cover", unreachable)
        assert await fi.resolve_artwork([str(music_dir)]) == 0
        monkeypatch.setattr(fi.artwork_resolver, "read_cover", real)

        assert await fi.resolve_artwork([str(music_dir)]) == 1


class TestWhenThingsGoWrong:
    @pytest.mark.asyncio
    async def test_a_folder_that_cannot_be_listed_keeps_its_cover(
        self, indexer, monkeypatch
    ):
        """Unlistable is not empty: the share may be back in a moment."""
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        _image(folder / "cover.jpg", 1)
        await fi.resolve_artwork([str(music_dir)])
        real = LocalStorage.listdir

        def listdir(storage, path):
            if path == str(folder):
                raise OSError("host is down")
            return real(storage, path)

        monkeypatch.setattr(LocalStorage, "listdir", listdir)
        await fi.resolve_artwork([str(music_dir)], dirty_dirs={str(folder)})

        assert (await db.get_album_by_id(album))["image_url"] == f"{album}.jpg"
        assert _origin(config, album)["source"] == "folder_art"

    @pytest.mark.asyncio
    async def test_an_unreadable_track_gives_way_to_the_next(
        self, indexer, monkeypatch
    ):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        first = _flac(folder / "01.flac", "Murder Ballads", 1, cover=patterned_png(4))
        album, _ = await _index(
            fi,
            first,
            _flac(folder / "02.flac", "Murder Ballads", 2, cover=patterned_png(4)),
        )
        _forget_first_covers(config)
        real = fi.artwork_resolver.read_cover

        def read(storage, path):
            if path == str(first):
                raise OSError("bad sector")
            return real(storage, path)

        monkeypatch.setattr(fi.artwork_resolver, "read_cover", read)

        assert await fi.resolve_artwork([str(music_dir)]) == 1
        assert _origin(config, album)["path"] == str(folder / "02.flac")

    @pytest.mark.asyncio
    async def test_a_watched_cover_removal_reopens_the_album_online_at_once(
        self, indexer, monkeypatch
    ):
        import kalinka_plugin_localfiles.indexer.indexer as indexer_module

        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        cover = _image(folder / "cover.jpg", 1)
        await fi.resolve_artwork([str(music_dir)])
        nudges, sweeps = [], []
        monkeypatch.setattr(
            indexer_module, "trigger_enricher_update", _record_async(nudges)
        )
        monkeypatch.setattr(fi, "cleanup_stale_tracks", _record_async(sweeps))
        cover.unlink()

        await fi.handle_incremental_changes({(ChangeKind.PATH_REMOVED, str(cover))})

        assert (await db.get_album_by_id(album))["image_url"] is None
        assert nudges == [("enrich",)]
        assert sweeps == []

    def test_a_nas_thumbnail_cache_is_not_watched(self, tmp_path):
        thumb = tmp_path / "Album" / "@eaDir" / "cover.jpg" / "SYNOPHOTO_THUMB_M.jpg"
        assert not _worth_indexing(ChangeKind.FILE_WRITTEN, str(thumb))


def _record_async(calls):
    async def record(*args):
        calls.append(args)
        return {}

    return record


class TestLibrariesIndexedBefore:
    def _forget_decisions(self, config):
        with closing(sqlite3.connect(config.db_path)) as conn:
            conn.execute("DELETE FROM resolved_origin WHERE field = 'image_url'")
            conn.commit()

    @pytest.mark.asyncio
    async def test_a_shared_folder_cover_lent_to_every_album_is_taken_back(
        self, indexer
    ):
        fi, db, music_dir, config = indexer
        mixed = music_dir / "Mixed"
        a, b = await _index(
            fi, _flac(mixed / "A.flac", "Album A"), _flac(mixed / "B.flac", "Album B")
        )
        cover = _image(mixed / "cover.jpg", 13)
        for album in (a, b):
            save_artwork_images(fi.artwork_path, cover.read_bytes(), album, "album")
            await db.update_album(album, {"image_url": f"{album}.jpg"})
        self._forget_decisions(config)

        await fi.resolve_artwork([str(music_dir)])

        assert (await db.get_album_by_id(a))["image_url"] is None
        assert (await db.get_album_by_id(b))["image_url"] is None

    @pytest.mark.asyncio
    async def test_an_online_cover_from_before_is_kept(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(fi, _flac(folder / "01.flac", "Murder Ballads"))
        _image(folder / "back.jpg", 13)
        save_artwork_images(fi.artwork_path, patterned_png(3), album, "album")
        await db.update_album(album, {"image_url": album})
        self._forget_decisions(config)

        await fi.resolve_artwork([str(music_dir)])

        assert (await db.get_album_by_id(album))["image_url"] == album

    @pytest.mark.asyncio
    async def test_a_cover_written_only_in_part_is_stored_again(self, indexer):
        fi, db, music_dir, config = indexer
        folder = music_dir / "Murder Ballads"
        (album,) = await _index(
            fi, _flac(folder / "01.flac", "Murder Ballads", cover=patterned_png(2))
        )
        await fi.resolve_artwork([str(music_dir)])
        (fi.artwork_path / "album" / f"{album}_large.jpg").unlink()

        assert await fi.resolve_artwork([str(music_dir)]) == 1


@pytest.mark.asyncio
async def test_a_full_scan_lists_an_album_folder_once(indexer, monkeypatch):
    """The cover search reads the listings the walk just took: on a share,
    every listing is a round trip."""
    fi, db, music_dir, config = indexer
    folder = music_dir / "Murder Ballads"
    _flac(folder / "01.flac", "Murder Ballads")
    await fi.run_scan()
    _image(folder / "cover.jpg", 1)
    listed = []
    real = LocalStorage.listdir

    def listdir(storage, path):
        listed.append(path)
        return real(storage, path)

    monkeypatch.setattr(LocalStorage, "listdir", listdir)
    await fi.run_scan()

    assert listed.count(str(folder)) == 1
    (album,) = {t["album_id"] for t in await db.get_all_tracks()}
    assert _stored_seed(fi, album, [1]) == 1
