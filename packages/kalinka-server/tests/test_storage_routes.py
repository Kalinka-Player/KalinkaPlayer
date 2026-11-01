"""``/server/storage``, and Drives as the running server offers it."""

import asyncio
import json
import os

import httpx
import pytest
from fastapi import FastAPI
from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.mounts import list_mounts
from kalinka_plugin_sdk.inputmodule import SourceUnavailableError
from kalinka_plugin_sdk.storage import LocationStatus, SourcesChange, StorageLocation

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig, StorageConfig
from kalinka_server.drives import ids
from kalinka_server.drives.folders import ServerFolder, ServerFolderList
from kalinka_server.storage.manager import Adopter, StorageManager
from kalinka_server.storage.requests import MountRequests
from kalinka_server.storage.routes import register_storage_routes
from tests.app_harness import isolate_app, running

MOUNTINFO = "\n".join(
    [
        "1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw",
        "50 1 8:1 / /media/kalinka/3A1F-9C2B ro - vfat /dev/sda1 ro",
    ]
)
FOLDER = EntityId(id="folder.x", type=EntityType.CATALOG, source="drives")
BLUES = EntityId(id="folder.y", type=EntityType.CATALOG, source="drives")


class _Library:
    def __init__(self, holds=False, source_id=None):
        self.holds = holds
        self.source_id = source_id

    async def location_status(self, location: StorageLocation):
        return LocationStatus(
            holds=self.holds,
            library_empty=True,
            removable=self.source_id is not None,
            restart_required=True,
        )

    async def adopt_location(self, location: StorageLocation):
        return SourcesChange(collection="music_sources", add={"kind": "local", "location": {"path": location.path}})

    async def release_location(self, location: StorageLocation):
        if self.source_id is None:
            return None
        return SourcesChange(collection="music_sources", remove=self.source_id)


async def _folders(path, offset, limit):
    if path == "/gone":
        raise SourceUnavailableError("SANDISK is not connected")
    if path not in (None, "/media/kalinka/3A1F-9C2B"):
        raise LookupError(path)
    folders = [ServerFolder(path="/media/kalinka/3A1F-9C2B", name="SANDISK", icon="usb_drive")] if path is None else []
    return ServerFolderList(offset=offset, limit=limit, total=len(folders), folders=folders)


@pytest.fixture
def library():
    return _Library()


@pytest.fixture
async def client(tmp_path, library):
    (tmp_path / "req").mkdir()
    (tmp_path / "res").mkdir()
    manager = StorageManager(
        config=lambda: StorageConfig(),
        media_dir=str(tmp_path / "nothing"),
        dispatch=lambda _event: None,
        adopters=lambda: [Adopter("localfiles", "My Library", library)],
        requests=MountRequests(str(tmp_path / "req"), str(tmp_path / "res")),
        mountinfo=lambda: list_mounts(MOUNTINFO),
        udev_data_dir=str(tmp_path),
        marker=str(tmp_path / "marker"),
        statvfs=lambda _p: (None, None),
    )
    await manager.refresh()
    saved = {
        "input_modules.localfiles.music_sources": [
            {"id": "media", "kind": "local"},
            {"id": "jazz", "kind": "local", "location": {"path": "/media/kalinka/3A1F-9C2B/Jazz"}},
        ]
    }
    applied = []

    async def apply(changes):
        applied.append(changes)
        # Saving takes a while, as checking a library's sources does.
        await asyncio.sleep(0.01)
        saved.update(changes)
        return {"message": "Ok", "issues": []}

    async def location_of(entity_id):
        if entity_id == FOLDER:
            return StorageLocation(volume_id="3A1F-9C2B", subpath="Jazz", path="/media/kalinka/3A1F-9C2B/Jazz", label="SANDISK")
        if entity_id == BLUES:
            return StorageLocation(volume_id="3A1F-9C2B", subpath="Blues", path="/media/kalinka/3A1F-9C2B/Blues", label="SANDISK")
        return None

    app = FastAPI()
    register_storage_routes(app, manager, location_of, saved.get, apply, _folders)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.applied = applied
        c.saved = saved
        c.requests_dir = tmp_path / "req"
        yield c


async def test_the_volumes_are_listed(client):
    volumes = (await client.get("/server/storage/volumes")).json()["volumes"]

    assert [(v["id"], v["kind"], v["library"]["holds"]) for v in volumes] == [("3A1F-9C2B", "removable", False)]


async def test_a_folder_is_located_with_what_the_library_makes_of_it(client):
    answer = (await client.get("/server/storage/location", params={"entity_id": FOLDER.to_string})).json()

    assert answer["volume_id"] == "3A1F-9C2B" and answer["subpath"] == "Jazz"
    assert answer["library"] == {
        "module": "localfiles",
        "title": "My Library",
        "holds": False,
        "library_empty": True,
        "removable": False,
        "restart_required": True,
    }


async def test_what_is_not_on_a_drive_has_no_location(client):
    other = EntityId(id="x", type=EntityType.CATALOG, source="qobuz").to_string

    assert (await client.get("/server/storage/location", params={"entity_id": other})).status_code == 404


async def test_adding_to_the_library_appends_its_record(client):
    answer = (await client.post("/server/storage/adopt", json={"entity_id": FOLDER.to_string})).json()

    assert answer == {"module": "localfiles", "issues": [], "restart_required": True}
    assert client.applied == [
        {
            "input_modules.localfiles.music_sources": [
                {"id": "media", "kind": "local"},
                {"id": "jazz", "kind": "local", "location": {"path": "/media/kalinka/3A1F-9C2B/Jazz"}},
                {"kind": "local", "location": {"path": "/media/kalinka/3A1F-9C2B/Jazz"}},
            ]
        }
    ]


async def test_two_folders_added_at_once_are_both_kept(client):
    await asyncio.gather(
        *(client.post("/server/storage/adopt", json={"entity_id": e.to_string}) for e in (FOLDER, BLUES))
    )

    paths = [r.get("location", {}).get("path") for r in client.saved["input_modules.localfiles.music_sources"]]
    assert paths[-2:] == ["/media/kalinka/3A1F-9C2B/Jazz", "/media/kalinka/3A1F-9C2B/Blues"]


async def test_a_folder_already_in_the_library_is_not_added_twice(client, library):
    library.holds = True

    await client.post("/server/storage/adopt", json={"entity_id": FOLDER.to_string})

    assert client.applied == []


async def test_a_folder_that_is_a_source_is_taken_out_of_the_library(client, library):
    library.holds, library.source_id = True, "jazz"

    located = (await client.get("/server/storage/location", params={"entity_id": FOLDER.to_string})).json()
    answer = (await client.post("/server/storage/release", json={"entity_id": FOLDER.to_string})).json()

    assert located["library"]["removable"] is True
    assert answer == {"module": "localfiles", "issues": [], "restart_required": True}
    assert client.applied == [{"input_modules.localfiles.music_sources": [{"id": "media", "kind": "local"}]}]


async def test_a_folder_held_through_one_above_it_is_not_taken_out(client, library):
    library.holds = True

    r = await client.post("/server/storage/release", json={"entity_id": FOLDER.to_string})

    assert r.status_code == 409 and "folder above it" in r.json()["detail"]
    assert client.applied == []


async def test_a_folder_not_in_the_library_has_nothing_to_take_out(client):
    answer = (await client.post("/server/storage/release", json={"entity_id": FOLDER.to_string})).json()

    assert answer["restart_required"] is False and client.applied == []


async def test_a_source_the_library_no_longer_has_is_left_alone(client, library):
    library.holds, library.source_id = True, "gone"

    answer = (await client.post("/server/storage/release", json={"entity_id": FOLDER.to_string})).json()

    assert answer["restart_required"] is False and client.applied == []


async def test_the_places_folders_can_be_in_are_listed(client):
    answer = (await client.get("/server/storage/folders")).json()

    assert answer["total"] == 1
    assert answer["folders"] == [
        {"path": "/media/kalinka/3A1F-9C2B", "name": "SANDISK", "subname": None, "icon": "usb_drive"}
    ]


async def test_folders_outside_the_places_or_unreadable_are_refused(client):
    assert (await client.get("/server/storage/folders", params={"path": "/etc"})).status_code == 404
    assert (await client.get("/server/storage/folders", params={"path": "/gone"})).status_code == 503


async def test_a_drive_is_ejected_through_the_helper(client):
    assert (await client.post("/server/storage/volumes/3A1F-9C2B/eject")).status_code == 202
    (asked,) = client.requests_dir.glob("*.3A1F-9C2B.json")
    assert json.loads(asked.read_text())["eject"] is True
    assert (await client.post("/server/storage/volumes/nope/eject")).status_code == 404


@pytest.fixture
async def app(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    from kalinka_plugin_sdk import paths

    os.makedirs(paths.media_dir())
    return await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})


async def test_the_server_offers_drives_and_says_it_has_storage(app):
    async with running(app) as client:
        version = (await client.get("/server/version")).json()
        modules = (await client.get("/server/modules")).json()["input_modules"]
        roots = (await client.get("/browse", params={"limit": 100})).json()["items"]
        volumes = (await client.get("/server/storage/volumes")).json()["volumes"]

    assert version["storage"] == {"volumes": True, "mounts": True, "adopt": True, "folders": True}
    drives = next(m for m in modules if m["name"] == "drives")
    assert drives["builtin"] is True and drives["title"] == "Drives"
    assert drives["icon"] == "storage_outlined"
    # Whatever this machine has plugged in.
    assert drives["count"] == app.state.drives.count()
    root = next(r for r in roots if r["id"] == "kalinka:drives:catalog:root")
    assert root["catalog"]["preview_config"]["type"] == "folder"
    assert root["catalog"]["live"] is True and root["catalog"]["on_storage"] is True
    assert volumes[0]["id"] == "internal"
    assert volumes[0]["summary"].startswith("On this server")


async def test_a_page_is_located_by_the_source_it_is_from(app):
    from kalinka_plugin_sdk import paths

    folder = os.path.join(paths.media_dir(), "Jazz")
    os.makedirs(folder)
    async with running(app) as client:
        located = await client.get(
            "/server/storage/location", params={"entity_id": ids.folder_id(folder).to_string}
        )
        elsewhere = await client.get(
            "/server/storage/location",
            params={"entity_id": EntityId(id="root", type=EntityType.CATALOG, source="collections").to_string},
        )

    assert located.status_code == 200
    assert (located.json()["volume_id"], located.json()["subpath"]) == ("internal", "Jazz")
    assert elsewhere.status_code == 404


async def test_the_picker_lists_the_server_folders_but_drives_does_not(app):
    from kalinka_plugin_sdk import paths

    os.makedirs(os.path.join(paths.media_dir(), "Jazz"))
    async with running(app) as client:
        places = (await client.get("/server/storage/folders")).json()["folders"]
        inside = (await client.get("/server/storage/folders", params={"path": places[0]["path"]})).json()

    assert places[0]["name"] == "Internal storage" and places[0]["icon"] == "internal_storage"
    assert [f["name"] for f in inside["folders"]] == ["Jazz"]


async def test_a_queued_drive_track_resolves_through_drives(app, tmp_path):
    from kalinka_server.player_setup import modules

    async with running(app):
        assert modules.set_up_input_module("drives") is app.state.drives
        assert modules.enabled_input_module("drives") is app.state.drives
        assert server.enabled_input_module("drives") is app.state.drives
