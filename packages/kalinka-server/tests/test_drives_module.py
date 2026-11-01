"""Drives: mounted volumes browsed and played straight from their files."""

import asyncio
import io
import json
import os
import time

import numpy as np
import pytest
import soundfile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from kalinka_plugin_sdk.datamodel import EntityType, PreviewType
from kalinka_plugin_sdk.inputmodule import ModuleCapability, SourceUnavailableError
from kalinka_plugin_sdk.storage import (
    StorageLocation,
    VolumeDescriptor,
    VolumeKind,
    VolumeState,
)

from kalinka_server.drives import fsio, ids, listing, rows
from kalinka_server.drives.listing import ListingCache, TagCache
from kalinka_server.drives.module import DrivesModule
from kalinka_server.storage.describe import summary_of


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (400, 400), (200, 30, 50)).save(out, format="PNG")
    return out.getvalue()


def _flac(path, cover=False, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    flac = FLAC(str(path))
    for key, value in tags.items():
        flac[key] = value
    if cover:
        picture = Picture()
        picture.type = PictureType.COVER_FRONT
        picture.mime = "image/png"
        picture.data = _png()
        flac.add_picture(picture)
    flac.save()
    return path


class _Storage:
    """The storage manager as Drives sees it."""

    def __init__(self, volumes, holds=()):
        self._volumes = volumes
        self.holds = set(holds)

    def volumes(self):
        return list(self._volumes)

    def volume(self, volume_id):
        return next((v for v in self._volumes if v.id == volume_id), None)

    def location_of(self, path):
        for v in self._volumes:
            if path == v.mount_point or path.startswith(v.mount_point + os.sep):
                rel = os.path.relpath(path, v.mount_point)
                return StorageLocation(volume_id=v.id, subpath="" if rel == "." else rel, path=path, label=v.label)
        return None

    async def library_holding(self, path):
        return "My Library" if path in self.holds else None


def _volume(path, volume_id, label, kind=VolumeKind.REMOVABLE, state=VolumeState.MOUNTED, **facts):
    volume = VolumeDescriptor(
        id=volume_id, kind=kind, label=label, mount_point=str(path), state=state,
        size_bytes=31_000_000_000, bus="usb",
    ).model_copy(update=facts)
    return volume.model_copy(update={"summary": summary_of(volume)})


@pytest.fixture
def stick(tmp_path):
    root = tmp_path / "stick"
    _flac(root / "Jazz" / "Kind of Blue" / "01 So What.flac", cover=True,
          title="So What", artist="Miles Davis", album="Kind of Blue", tracknumber="1", date="1959")
    _flac(root / "Jazz" / "Kind of Blue" / "02 Freddie Freeloader.flac")
    (root / "Jazz" / "Kind of Blue" / "cover.png").write_bytes(_png())
    _flac(root / "Loose.flac", title="Loose")
    (root / "notes.txt").write_text("not music")
    (root / "._Loose.flac").write_bytes(b"")
    (root / "System Volume Information").mkdir()
    (root / "Empty").mkdir()
    return root


def _drives(tmp_path, volumes, enabled=True, holds=()):
    return DrivesModule(_Storage(volumes, holds), str(tmp_path / "cache"), enabled=lambda: enabled)


async def test_one_drive_opens_on_its_own_folders_then_its_tracks(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    listing = await drives.browse(ids.catalog_id(ids.ROOT))

    assert [item.name for item in listing.items] == ["Empty", "Jazz", "Loose"]
    assert listing.total == 3
    jazz = listing.items[1]
    assert jazz.subname == "1 folder" and jazz.catalog.preview_config.type == PreviewType.FOLDER
    assert listing.items[0].subname == "Empty" and not listing.items[0].can_add
    assert listing.items[2].track.title == "Loose"


async def test_tracks_carry_their_tags_and_covers(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])
    album = stick / "Jazz" / "Kind of Blue"

    listing = await drives.browse(ids.folder_id(str(album)))

    so_what, freddie = [item.track for item in listing.items]
    assert (so_what.title, so_what.performer.name, so_what.album.title, so_what.track_number) == (
        "So What", "Miles Davis", "Kind of Blue", 1,
    )
    assert "/e." in so_what.album.image.small
    assert freddie.title == "Freddie Freeloader" and freddie.performer is None
    assert "/f." in freddie.album.image.small
    assert freddie.album.title == "Kind of Blue"


async def test_several_drives_are_listed_one_each(tmp_path, stick):
    other = tmp_path / "other"
    other.mkdir()
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK"), _volume(other, "rec_nas", "nas", VolumeKind.MOUNT)])

    listing = await drives.browse(ids.catalog_id(ids.ROOT))

    assert [(i.name, i.subname) for i in listing.items] == [("SANDISK", "USB drive · 31 GB"), ("nas", "USB drive · 31 GB")]
    assert listing.items[0].id == ids.volume_id("3A1F")


async def test_each_row_names_what_to_draw_where_it_has_no_art(tmp_path, stick):
    (tmp_path / "nas").mkdir()
    (tmp_path / "disk").mkdir()
    share = _volume(tmp_path / "nas", "rec_nas", "nas", VolumeKind.MOUNT, fs_type="nfs4", bus=None)
    archive = _volume(tmp_path / "disk", "rec_disk", "Archive", VolumeKind.MOUNT, bus="ata")
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK"), share, archive])

    volumes = (await drives.browse(ids.catalog_id(ids.ROOT))).items
    on_stick = (await drives.browse(ids.volume_id("3A1F"))).items

    assert {v.name: (v.icon, v.subname) for v in volumes} == {
        "SANDISK": ("usb_drive", "USB drive · 31 GB"),
        "nas": ("network_share", "NFS share · 31 GB"),
        "Archive": ("disk", "Disk · 31 GB"),
    }
    assert [item.icon for item in on_stick] == ["folder", "folder", "track"]


async def test_catalogs_follow_whether_a_drive_is_mounted(tmp_path, stick):
    assert _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")]).capabilities() == {ModuleCapability.CATALOGS}
    internal = _volume(tmp_path, "internal", "Internal storage", VolumeKind.INTERNAL)
    assert _drives(tmp_path, [internal]).capabilities() == frozenset()
    gone = _volume(stick, "3A1F", "SANDISK", state=VolumeState.UNMOUNTED)
    assert _drives(tmp_path, [gone]).capabilities() == frozenset()


async def test_switched_off_drives_list_and_count_nothing(tmp_path, stick):
    internal = _volume(tmp_path / "music", "internal", "Internal storage", VolumeKind.INTERNAL)
    drives = _drives(tmp_path, [internal, _volume(stick, "3A1F", "SANDISK")], enabled=False)

    assert (await drives.browse(ids.catalog_id(ids.ROOT))).total == 0
    assert drives.capabilities() == frozenset()
    assert drives.count() is None


async def test_the_drives_are_counted_for_the_source_icon(tmp_path, stick):
    internal = _volume(tmp_path / "music", "internal", "Internal storage", VolumeKind.INTERNAL)
    other = tmp_path / "other"
    other.mkdir()
    drives = _drives(tmp_path, [internal, _volume(stick, "3A1F", "SANDISK"), _volume(other, "77AA", "KINGSTON")])

    assert drives.count() == 2
    assert drives.registered_source().count() == 2


async def test_drive_pages_change_without_kalinka_and_are_on_storage(tmp_path, stick):
    other = tmp_path / "other"
    other.mkdir()
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK"), _volume(other, "77AA", "KINGSTON")])
    entry = drives.registered_source()

    volumes = (await drives.browse(ids.catalog_id(ids.ROOT))).items
    folders = (await drives.browse(ids.volume_id("3A1F"))).items

    assert (entry.live, entry.on_storage, entry.icon) == (True, True, "storage_outlined")
    assert all(item.catalog.live and item.catalog.on_storage for item in [*volumes, *folders[:2]])


async def test_home_folders_are_not_browsed_or_played_through_drives(tmp_path, stick):
    home = tmp_path / "home"
    _flac(home / "alice" / "song.flac")
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    with pytest.raises(SourceUnavailableError):
        await drives.browse(ids.folder_id(str(home / "alice")))
    with pytest.raises(SourceUnavailableError):
        await drives.get_content_info(ids.track_id(str(home / "alice" / "song.flac")).id)


async def test_a_folder_in_the_library_says_so(tmp_path, stick):
    jazz = str(stick / "Jazz")
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")], holds=[jazz])

    listing = await drives.browse(ids.catalog_id(ids.ROOT))

    assert listing.items[1].subname == "1 folder · In My Library"


async def test_a_track_plays_from_its_file(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])
    track = ids.track_id(str(stick / "Loose.flac")).id

    source = await drives.get_track_source(track)
    info = await drives.get_content_info(track)

    assert source.source.module == "drives" and source.format == "audio/flac"
    assert info.local_path == str(stick / "Loose.flac")


async def test_nothing_off_the_drives_is_served(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])
    elsewhere = tmp_path / "secret.flac"
    _flac(elsewhere)

    with pytest.raises(SourceUnavailableError):
        await drives.get_content_info(ids.track_id(str(elsewhere)).id)
    # Not audio, or gone: nothing there to play, which /content answers 404.
    assert await drives.get_content_info(ids.track_id(str(stick / "notes.txt")).id) is None
    assert await drives.get_content_info(ids.track_id(str(stick / "Gone.flac")).id) is None
    with pytest.raises(SourceUnavailableError):
        await drives.browse(ids.folder_id("/etc"))


async def test_a_track_on_an_unplugged_drive_names_the_drive(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK", state=VolumeState.UNMOUNTED)])

    with pytest.raises(SourceUnavailableError, match="SANDISK is not connected"):
        await drives.get_track_source(ids.track_id(str(stick / "Loose.flac")).id)


async def test_adding_a_folder_takes_everything_below_it(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    added = await drives.tracks_to_add(ids.volume_id("3A1F"), limit=100)
    items = await drives.get_all(added)

    assert [i.track.title for i in items] == ["So What", "Freddie Freeloader", "Loose"]
    assert all(i.id.type == EntityType.TRACK for i in items)


async def test_an_album_is_its_folders_tracks(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    listing = await drives.browse(ids.album_id(str(stick / "Jazz")))

    assert listing.items == []


async def test_a_cover_is_made_from_the_file_on_request(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])
    track = stick / "Jazz" / "Kind of Blue" / "01 So What.flac"
    folder = track.parent

    embedded = await drives.get_resource_path(f"drives/e.{ids.encode_path(str(track))}_small.jpg")
    beside = await drives.get_resource_path(f"drives/f.{ids.encode_path(str(folder))}_thumbnail.jpg")

    with Image.open(embedded) as image:
        assert image.format == "JPEG" and max(image.size) == 230
    with Image.open(beside) as image:
        assert max(image.size) == 50
    secret = tmp_path / "secret.flac"
    _flac(secret, cover=True)
    assert await drives.get_resource_path(f"drives/e.{ids.encode_path(str(secret))}_small.jpg") is None
    assert await drives.get_resource_path("album/x_small.jpg") is None


async def test_a_cover_linked_from_off_the_drive_is_not_shown(tmp_path, stick):
    outside = tmp_path / "private.png"
    outside.write_bytes(_png())
    folder = stick / "Linked"
    folder.mkdir()
    (folder / "cover.png").symlink_to(outside)
    _flac(folder / "01 Untagged.flac")
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    assert await drives.get_resource_path(f"drives/f.{ids.encode_path(str(folder))}_small.jpg") is None


async def test_a_drive_that_stops_answering_is_unavailable_not_a_hang(tmp_path, stick, monkeypatch):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])
    monkeypatch.setattr(fsio, "FS_TIMEOUT_S", 0.1)
    monkeypatch.setattr(drives._listings, "list", lambda _path: time.sleep(1))

    started = time.monotonic()
    with pytest.raises(SourceUnavailableError, match="not answering"):
        await drives.browse(ids.folder_id(str(stick / "Jazz")))
    assert time.monotonic() - started < 0.9


async def test_folders_a_slow_drive_cannot_count_in_time_are_listed_without_counts(
    tmp_path, stick, monkeypatch
):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])
    monkeypatch.setattr(fsio, "COUNT_BUDGET_S", 0.05)
    monkeypatch.setattr(rows, "count_folder", lambda _path: time.sleep(0.5))

    listing = await drives.browse(ids.catalog_id(ids.ROOT))

    empty, jazz = listing.items[:2]
    assert (jazz.name, jazz.subname, jazz.can_add) == ("Jazz", None, True)
    assert empty.subname is None


async def test_a_malformed_file_or_a_name_that_is_not_utf8_lists_all_the_same(tmp_path, stick):
    odd = os.fsdecode(b"Caf\xe9")
    (stick / odd).mkdir()
    (stick / "Broken.m4a").write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 40)
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    listing = await drives.browse(ids.catalog_id(ids.ROOT))

    names = [item.name for item in listing.items]
    assert "Caf\ufffd" in names and "Broken" in names
    json.dumps(listing.model_dump(mode="json"))


async def test_a_file_that_would_not_read_is_read_again_next_time(tmp_path, monkeypatch):
    track = _flac(tmp_path / "So What.flac", title="So What")
    entry = listing.entry_of(str(track))
    tags = TagCache()
    real = listing.read_summary

    def unplugged(_path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(listing, "read_summary", unplugged)
    assert await tags.read_within([entry], 5.0) == {str(track): None}
    monkeypatch.setattr(listing, "read_summary", real)
    assert (await tags.read_within([entry], 5.0))[str(track)].title == "So What"
    tags.shutdown()


async def test_a_drive_that_stops_answering_holds_up_no_other(tmp_path, stick, monkeypatch):
    dead = tmp_path / "nas"
    dead.mkdir()
    io = fsio.DriveIO(lane_of=lambda path: "nas" if path.startswith(str(dead)) else "stick")
    monkeypatch.setattr(fsio, "FS_TIMEOUT_S", 0.2)
    hung = [asyncio.ensure_future(io.run(time.sleep, 1.0, at=str(dead))) for _ in range(6)]

    started = time.monotonic()
    assert await io.run(os.path.isdir, str(stick), at=str(stick)) is True
    assert time.monotonic() - started < 0.15
    await asyncio.gather(*hung, return_exceptions=True)
    io.close()


def test_a_folder_says_whether_it_holds_a_cover(tmp_path, stick):
    assert listing.folder_has_images(str(stick / "Jazz" / "Kind of Blue"))
    assert not listing.folder_has_images(str(stick / "Jazz"))
    assert not listing.folder_has_images(str(tmp_path / "missing"))


def test_a_folder_is_listed_again_only_once_something_in_it_changed(tmp_path, monkeypatch):
    folder = tmp_path / "Jazz"
    _flac(folder / "01.flac")
    cache = ListingCache()
    listed = []
    real = listing.list_folder
    monkeypatch.setattr(listing, "list_folder", lambda p: listed.append(p) or real(p))

    cache.list(str(folder))
    cache.list(str(folder))
    assert len(listed) == 1
    _flac(folder / "02.flac")
    os.utime(folder, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert len(cache.list(str(folder)).tracks) == 2
    assert len(listed) == 2


async def test_where_a_page_is_on_storage(tmp_path, stick):
    drives = _drives(tmp_path, [_volume(stick, "3A1F", "SANDISK")])

    located = await drives.storage_location(ids.folder_id(str(stick / "Jazz")))

    assert (located.volume_id, located.subpath) == ("3A1F", "Jazz")
    assert await drives.storage_location(ids.folder_id("/etc")) is None
