"""What My Library says of a folder offered to it from a drive, and how it
takes one in or lets one go."""

from types import SimpleNamespace

import pytest

from kalinka_plugin_sdk.storage import LibraryAdopter, StorageLocation

from kalinka_plugin_localfiles.config_model import LocalFilesConfig, LocalLocation, LocalSource
from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles


def _plugin(*roots):
    plugin = KalinkaPluginLocalFiles()
    plugin._context = SimpleNamespace(
        config=LocalFilesConfig(
            music_sources=[LocalSource(id=f"s{i}", location=LocalLocation(path=str(r))) for i, r in enumerate(roots)]
        )
    )
    return plugin


def _at(path, subpath="Jazz"):
    return StorageLocation(volume_id="3A1F", subpath=subpath, path=str(path))


def test_the_library_opts_in_to_keeping_folders_from_drives():
    assert isinstance(KalinkaPluginLocalFiles(), LibraryAdopter)


@pytest.mark.asyncio
async def test_a_new_folder_is_offered_as_a_folder_source(tmp_path):
    jazz = tmp_path / "stick" / "Jazz"
    jazz.mkdir(parents=True)
    plugin = _plugin(tmp_path / "music")

    status = await plugin.location_status(_at(jazz))
    change = await plugin.adopt_location(_at(jazz))

    assert not status.holds and not status.removable
    assert status.library_empty and status.restart_required
    assert change.collection == "music_sources"
    assert change.add == {"kind": "local", "location": {"path": str(jazz)}}
    LocalFilesConfig(music_sources=[change.add])


@pytest.mark.asyncio
async def test_a_folder_under_a_source_is_held_but_not_removable_there(tmp_path):
    album = tmp_path / "stick" / "Jazz" / "Kind of Blue"
    album.mkdir(parents=True)
    plugin = _plugin(tmp_path / "stick")

    status = await plugin.location_status(_at(album, "Jazz/Kind of Blue"))

    assert status.holds and not status.removable
    assert await plugin.release_location(_at(album, "Jazz/Kind of Blue")) is None


@pytest.mark.asyncio
async def test_a_folder_that_is_a_source_is_released_by_its_id(tmp_path):
    jazz = tmp_path / "stick" / "Jazz"
    jazz.mkdir(parents=True)
    plugin = _plugin(tmp_path / "music", jazz)

    status = await plugin.location_status(_at(jazz))
    change = await plugin.release_location(_at(jazz))

    assert status.holds and status.removable
    assert change.collection == "music_sources" and change.remove == "s1" and change.add is None


@pytest.mark.asyncio
async def test_what_is_not_a_path_on_this_server_is_declined(tmp_path):
    plugin = _plugin(tmp_path)
    elsewhere = StorageLocation(volume_id="x", path="Jazz")

    assert await plugin.location_status(elsewhere) is None
    assert await plugin.adopt_location(elsewhere) is None
    assert await plugin.release_location(elsewhere) is None
