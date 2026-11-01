"""The server's folders as Settings browses them to pick a music folder."""

import os

import pytest

from kalinka_plugin_sdk.inputmodule import SourceUnavailableError
from kalinka_plugin_sdk.storage import VolumeDescriptor, VolumeKind, VolumeState

from kalinka_server.drives.folders import ServerFolders
from kalinka_server.drives.fsio import DriveIO
from kalinka_server.storage.describe import summary_of


class _Storage:
    def __init__(self, volumes, holds=()):
        self._volumes = volumes
        self.holds = set(holds)

    def volumes(self):
        return list(self._volumes)

    async def library_holding(self, path):
        return "My Library" if path in self.holds else None


def _volume(path, volume_id, label, kind=VolumeKind.REMOVABLE, state=VolumeState.MOUNTED):
    volume = VolumeDescriptor(
        id=volume_id, kind=kind, label=label, mount_point=str(path), state=state, size_bytes=31_000_000_000, bus="usb"
    )
    return volume.model_copy(update={"summary": summary_of(volume)})


@pytest.fixture
def tree(tmp_path):
    stick = tmp_path / "stick"
    (stick / "Jazz" / "Kind of Blue").mkdir(parents=True)
    (stick / "Jazz" / "Kind of Blue" / "01.flac").write_bytes(b"")
    (stick / "Loose.flac").write_bytes(b"")
    (stick / "System Volume Information").mkdir()
    (tmp_path / "home" / "alice").mkdir(parents=True)
    (tmp_path / "music").mkdir()
    return tmp_path


def _folders(tree, volumes=None, holds=()):
    volumes = volumes or [
        _volume(tree / "music", "internal", "Internal storage", VolumeKind.INTERNAL),
        _volume(tree / "stick", "3A1F", "SANDISK"),
        _volume(tree / "gone", "77AA", "KINGSTON", state=VolumeState.UNMOUNTED),
    ]
    return ServerFolders(_Storage(volumes, holds), DriveIO(), home=str(tree / "home"))


async def test_the_places_are_the_mounted_volumes_then_home(tree):
    places = (await _folders(tree).list()).folders

    assert [(p.name, p.icon) for p in places] == [
        ("Internal storage", "internal_storage"),
        ("SANDISK", "usb_drive"),
        ("Home folders", "folder"),
    ]
    assert places[1].subname == "USB drive · 31 GB"
    assert places[2].path == str(tree / "home")


async def test_a_folder_lists_its_folders_and_no_files(tree):
    listing = await _folders(tree, holds=[str(tree / "stick" / "Jazz")]).list(str(tree / "stick"))

    assert listing.total == 1
    (jazz,) = listing.folders
    assert (jazz.name, jazz.path) == ("Jazz", str(tree / "stick" / "Jazz"))
    assert jazz.subname == "1 folder · In My Library"


async def test_home_folders_can_be_walked(tree):
    listing = await _folders(tree).list(str(tree / "home"))

    assert [f.name for f in listing.folders] == ["alice"]


async def test_nothing_outside_the_places_is_listed(tree):
    (tree / "etc").mkdir()
    os.symlink(tree / "etc", tree / "stick" / "escape")

    with pytest.raises(LookupError):
        await _folders(tree).list(str(tree / "etc"))
    with pytest.raises(LookupError):
        await _folders(tree).list(str(tree / "stick" / "escape"))
    with pytest.raises(LookupError):
        await _folders(tree).list(str(tree / "gone"))
    with pytest.raises(LookupError):
        await _folders(tree).list("stick/Jazz")
    with pytest.raises(LookupError):
        await _folders(tree).list(str(tree / "stick") + "\0")


async def test_a_folder_that_cannot_be_read_is_unavailable(tree):
    with pytest.raises(SourceUnavailableError, match="cannot be read"):
        await _folders(tree).list(str(tree / "stick" / "missing"))
