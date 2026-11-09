"""What Drives & shares lists, and what each action asks for."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from kalinka_plugin_sdk.mounts import list_mounts

from kalinka_server.config_model import DiskMount, NfsMount, NfsVersion, StorageConfig
from kalinka_server.storage.device_routes import register_device_routes
from kalinka_server.storage.devices import (
    DRIVE_FAILED,
    NO_HELPER,
    SHARE_FAILED,
    DeviceChanges,
    DeviceError,
    NewShare,
    StorageDevices,
    address_seen_by,
    nfs_id,
)
from kalinka_server.storage.manager import StorageManager
from kalinka_server.storage.nfs_discovery import Export, HostAnswer, ScanState
from kalinka_server.storage import requests as requests_mod
from kalinka_server.storage.requests import UNSENT, MountRequests

MUSIC = nfs_id("nas.local", "/volume1/music")

MOUNTINFO = "\n".join(
    [
        "1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw",
        "50 1 8:1 / /media/kalinka/3A1F-9C2B ro,nosuid - vfat /dev/sda1 ro",
        "51 1 8:17 / /mnt/archive ro - ext4 /dev/sdb1 ro",
        f"52 1 0:60 / /media/kalinka/{MUSIC} ro - nfs4 nas.local:/volume1/music ro",
    ]
)

UDEV = {
    "b8:1": "E:ID_FS_LABEL=SANDISK\nE:ID_FS_UUID=3A1F-9C2B\nE:ID_FS_TYPE=vfat\nE:ID_FS_USAGE=filesystem\nE:ID_BUS=usb\n",
    "b8:17": "E:ID_FS_LABEL=Archive\nE:ID_FS_UUID=aaaa-bbbb\nE:ID_FS_TYPE=ext4\nE:ID_FS_USAGE=filesystem\nE:ID_BUS=ata\n",
    "b8:33": "E:ID_FS_LABEL=Spare\nE:ID_FS_UUID=1111-2222\nE:ID_FS_TYPE=ext4\nE:ID_FS_USAGE=filesystem\nE:ID_BUS=ata\n",
    "b8:49": "E:ID_FS_UUID=9999-0000\nE:ID_FS_TYPE=vfat\nE:ID_FS_USAGE=filesystem\nE:ID_BUS=usb\n",
}


class _Finder:
    def __init__(self):
        self.found: list[Export] = []
        self.probed: list[str] = []
        self.rescans = 0

    def exports(self):
        return list(self.found)

    def scan_state(self):
        return ScanState(running=False, finished_at=1762171200.0)

    def probe(self, host):
        self.probed.append(host)
        return HostAnswer(host=host, reachable=True, exports=(Export(host=host, path="/flac"),))

    def rescan(self):
        self.rescans += 1


class _Machine:
    def __init__(self, tmp_path, config=None, mountinfo=MOUNTINFO, answer_wait=0):
        self.config = config or StorageConfig(
            mounts=[NfsMount(id=MUSIC, label="Music", host="nas.local", export="/volume1/music")]
        )
        self.refused = None
        self.requests_dir = tmp_path / "requests"
        self.requests_dir.mkdir()
        self.results_dir = tmp_path / "results"
        self.results_dir.mkdir()
        udev = tmp_path / "udev"
        udev.mkdir()
        for name, text in UDEV.items():
            (udev / name).write_text(text)
        self.mountinfo = mountinfo
        self.manager = StorageManager(
            config=lambda: self.config,
            media_dir=str(tmp_path / "music"),
            dispatch=lambda _event: None,
            requests=MountRequests(str(self.requests_dir), str(tmp_path / "results")),
            mountinfo=lambda: list_mounts(self.mountinfo),
            udev_data_dir=str(udev),
            marker=str(tmp_path / "state" / "no-automount"),
            remembered=str(tmp_path / "state" / "mount-when-present"),
            statvfs=lambda _p: (31_000_000_000, 12_000_000_000),
            fstab=str(tmp_path / "fstab"),
            sys_dev_block=str(tmp_path / "sysdev"),
        )
        (tmp_path / "music").mkdir()
        self.finder = _Finder()
        self.saved = []
        self.devices = StorageDevices(
            self.manager,
            lambda: self.config,
            self.finder,
            self._save,
            answer_wait=answer_wait,
            seen_by=lambda _host: "192.168.1.50",
        )

    async def _save(self, changes):
        if self.refused:
            return self.refused
        self.saved.append(changes)
        self.config = StorageConfig(
            automount_usb=self.config.automount_usb, mounts=changes[StorageDevices.MOUNTS_PATH]
        )
        await self.manager.config_changed()
        return None

    async def ready(self):
        """With the records saved while the server runs, which mounts none."""
        config, self.config = self.config, StorageConfig(automount_usb=self.config.automount_usb)
        await self.manager.config_changed()
        self.config = config
        await self.manager.config_changed()
        return self

    def device(self, device_id):
        return next(d for d in self.devices.listing().devices if d.id == device_id)

    def ids(self):
        return [d.id for d in self.devices.listing().devices]

    def requests(self, volume_id):
        asked = [json.loads(p.read_text()) for p in sorted(self.requests_dir.glob(f"*.{volume_id}.json"))]
        return [{k: v for k, v in a.items() if k not in ("seq", "id")} for a in asked]


async def _helper(machine, ok=True, error=None):
    """Answers every request as the root helper does: carries a mount or an
    unmount out where it agrees, then says so."""
    answered = set()
    while True:
        for path in sorted(machine.requests_dir.glob("*.json")):
            if path.name in answered:
                continue
            answered.add(path.name)
            request = json.loads(path.read_text())
            mount_point = f"/media/kalinka/{request['id']}"
            if ok and request["action"] == "mount":
                machine.mountinfo += f"\n60 1 8:33 / {mount_point} ro - ext4 /dev/sdc1 ro"
            elif ok:
                machine.mountinfo = "\n".join(
                    line for line in machine.mountinfo.split("\n") if f" {mount_point} " not in line
                )
            # Replaced whole, as the helper does: a listing reads it off the loop.
            partial = machine.results_dir / f".{request['id']}.part"
            partial.write_text(json.dumps({"ok": ok, "error": error, "seq": request["seq"], "at": time.time()}))
            partial.replace(machine.results_dir / f"{request['id']}.json")
        await asyncio.sleep(0.01)


async def _with_helper(machine, action, **answer):
    helper = asyncio.ensure_future(_helper(machine, **answer))
    try:
        return await action
    finally:
        helper.cancel()


async def _kept(machine, device_id):
    """Waits for ``device_id`` to be kept once its mount came through."""
    for _ in range(200):
        await machine.manager.refresh()
        if any(r.id == device_id for r in machine.config.mounts):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{device_id} was never kept")


def _actions(device):
    return {
        name
        for name in ("mount", "unmount", "eject", "set_at_start", "edit", "forget", "dismiss")
        if getattr(device, f"can_{name}")
    }


@pytest.fixture
async def machine(tmp_path):
    return await _Machine(tmp_path).ready()


async def test_every_drive_and_share_is_listed_once_mounted_or_not(machine):
    listing = machine.devices.listing()

    assert [(d.id, d.medium.value, d.label, d.state.value) for d in listing.devices] == [
        ("internal", "internal", "Internal storage", "mounted"),
        ("aaaa-bbbb", "disk", "Archive", "mounted"),
        ("1111-2222", "disk", "Spare", "unmounted"),
        ("3A1F-9C2B", "usb", "SANDISK", "mounted"),
        (MUSIC, "network", "Music", "mounted"),
    ]
    assert (listing.scan.running, listing.scan.finished_at) == (False, 1762171200.0)


async def test_internal_storage_and_what_the_system_mounted_have_no_actions(machine):
    internal, archive = machine.device("internal"), machine.device("aaaa-bbbb")

    assert _actions(internal) == set() and internal.at_start is None
    assert internal.summary == "12 GB free"
    assert (archive.managed_by, _actions(archive), archive.at_start) == ("system", set(), None)


async def test_a_disk_nothing_mounted_lists_the_switch_mounting_it_would_keep_it_with(machine):
    spare = machine.device("1111-2222")

    assert _actions(spare) == {"mount", "set_at_start", "edit"}
    assert (spare.saved, spare.at_start, spare.summary) == (False, True, "Disk")


async def test_with_automount_on_a_usb_drive_offers_only_eject(machine):
    stick = machine.device("3A1F-9C2B")

    assert _actions(stick) == {"eject"} and stick.at_start is None
    assert stick.summary.startswith("USB drive")
    assert "9999-0000" not in machine.ids()


async def test_with_automount_off_a_usb_drive_can_be_mounted_and_kept(tmp_path):
    machine = await _Machine(
        tmp_path, config=StorageConfig(automount_usb=False), mountinfo="1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw"
    ).ready()

    stick = machine.device("9999-0000")

    assert stick.medium.value == "usb"
    assert _actions(stick) == {"mount", "set_at_start", "edit"} and stick.at_start is False


async def test_a_usb_drive_mounted_with_automount_off_can_be_unmounted_or_ejected(tmp_path):
    machine = await _Machine(tmp_path, config=StorageConfig(automount_usb=False)).ready()

    stick = machine.device("3A1F-9C2B")

    assert _actions(stick) == {"unmount", "eject", "set_at_start", "edit"} and stick.at_start is False


async def test_a_saved_share_says_it_is_nfs_and_who_serves_it(machine):
    music = machine.device(MUSIC)

    assert _actions(music) == {"unmount", "set_at_start", "edit", "forget"}
    assert (music.summary, music.saved, music.at_start, music.found) == ("NFS · nas.local", True, True, False)
    assert (music.nfs.host, music.nfs.export, music.nfs.version) == ("nas.local", "/volume1/music", NfsVersion.v4_2)


async def test_a_saved_share_the_search_saw_is_one_row_however_its_host_was_named(machine):
    machine.finder.found = [
        Export(host="192.168.1.20", path="/volume1/music", server="DS920", clients=("192.168.1.0/24",), aliases=frozenset({"192.168.1.20", "nas.local"})),
        Export(host="192.168.1.20", path="/volume1/video", server="DS920", aliases=frozenset({"nas.local"})),
    ]

    devices = {d.id: d for d in machine.devices.listing().devices if d.medium.value == "network"}

    video = nfs_id("192.168.1.20", "/volume1/video")
    assert list(devices) == [MUSIC, video]
    assert (devices[MUSIC].found, devices[MUSIC].server, devices[MUSIC].clients) == (True, "DS920", ["192.168.1.0/24"])
    found = devices[video]
    assert (found.label, found.summary, found.saved, found.at_start) == ("video", "NFS · DS920", False, True)
    assert devices[MUSIC].summary == "NFS · DS920"
    assert _actions(found) == {"mount", "set_at_start", "edit"}


async def test_a_saved_drive_that_is_not_plugged_in_can_only_be_forgotten(tmp_path):
    machine = await _Machine(
        tmp_path,
        config=StorageConfig(
            automount_usb=False, mounts=[DiskMount(id="AB12-CD34", fs_uuid="AB12-CD34", usb=True, at_start=True)]
        ),
    ).ready()

    away = machine.device("AB12-CD34")

    assert (away.medium.value, away.connected, away.at_start) == ("usb", False, True)
    assert _actions(away) == {"set_at_start", "edit", "forget"}


async def test_a_saved_disk_that_is_present_shows_what_it_is(tmp_path):
    machine = await _Machine(tmp_path, config=StorageConfig(mounts=[DiskMount(id="rec_old", fs_uuid="1111-2222")])).ready()

    old = machine.device("rec_old")

    assert (old.label, old.connected, old.saved) == ("Spare", True, True)
    assert _actions(old) == {"mount", "set_at_start", "edit", "forget"}
    # The filesystem is the record's, not a second row.
    assert "1111-2222" not in machine.ids()


async def test_a_disk_mounted_from_here_is_kept_with_its_switch_on(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()

    result = await _with_helper(machine, machine.devices.mount("1111-2222"))

    assert result.state.value == "mounted"
    (record,) = [r for r in machine.config.mounts if isinstance(r, DiskMount)]
    assert (record.id, record.fs_uuid, record.at_start, record.usb, record.read_only) == (
        "1111-2222", "1111-2222", True, False, True,
    )
    assert machine.requests("1111-2222") == [
        {"action": "mount", "fstype": "disk", "source": "UUID=1111-2222", "options": ["ro"]}
    ]


async def test_a_device_not_kept_is_kept_only_once_its_mount_comes_through(machine):
    result = await machine.devices.mount("1111-2222")

    assert result.state.value == "mounting"
    assert machine.saved == []
    assert (machine.device("1111-2222").state.value, machine.device("1111-2222").saved) == ("mounting", False)

    await _with_helper(machine, _kept(machine, "1111-2222"))
    assert machine.device("1111-2222").saved


async def test_a_failed_mount_keeps_nothing_and_says_why_until_put_away(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video", server="DS920")]
    video = nfs_id("192.168.1.20", "/volume1/video")
    said = "mount.nfs4: access denied by server while mounting 192.168.1.20:/volume1/video"

    await _with_helper(machine, machine.devices.mount(video), ok=False, error=said)

    assert machine.saved == []
    failed = machine.device(video)
    assert (failed.saved, failed.state.value, failed.error_detail) == (False, "failed", said)
    assert failed.error.startswith("DS920 does not let this streamer in")
    assert _actions(failed) == {"mount", "set_at_start", "edit", "dismiss"}

    await machine.devices.dismiss(video)
    found = machine.device(video)
    assert (found.state.value, found.error, found.can_dismiss) == ("unmounted", None, False)


async def test_trying_again_mounts_with_what_was_tried(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video")]
    video = nfs_id("192.168.1.20", "/volume1/video")
    await _with_helper(
        machine, machine.devices.mount(video, DeviceChanges(label="Films", read_only=False)), ok=False, error="no"
    )
    assert (machine.device(video).label, machine.device(video).read_only) == ("Films", False)

    await _with_helper(machine, machine.devices.mount(video))

    record = next(r for r in machine.config.mounts if r.id == video)
    assert (record.label, record.read_only) == ("Films", False)


async def test_mounting_a_usb_drive_keeps_nothing(tmp_path):
    machine = await _Machine(tmp_path, config=StorageConfig(automount_usb=False)).ready()

    await machine.devices.mount("9999-0000")

    assert machine.saved == []
    assert machine.requests("9999-0000")[0]["source"] == "UUID=9999-0000"


async def test_mounting_a_found_share_keeps_it_named_after_its_folder(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video")]
    video = nfs_id("192.168.1.20", "/volume1/video")

    await _with_helper(machine, machine.devices.mount(video))

    record = next(r for r in machine.config.mounts if r.id == video)
    assert (record.label, record.host, record.export, record.version, record.at_start) == (
        "video", "192.168.1.20", "/volume1/video", NfsVersion.v4, True,
    )
    assert machine.requests(video)[0]["source"] == "192.168.1.20:/volume1/video"


async def test_unmounting_keeps_the_record_and_its_switch(machine):
    await machine.devices.unmount(MUSIC)

    assert machine.requests(MUSIC) == [{"action": "unmount", "eject": False}]
    assert machine.saved == []
    (record,) = machine.config.mounts
    assert (record.id, record.at_start) == (MUSIC, True)


async def test_mounting_a_kept_device_leaves_its_switch_as_it_was(tmp_path):
    machine = await _Machine(
        tmp_path, config=StorageConfig(mounts=[DiskMount(id="1111-2222", fs_uuid="1111-2222", at_start=False)])
    ).ready()

    await machine.devices.mount("1111-2222")

    (record,) = machine.config.mounts
    assert record.at_start is False and machine.saved == []
    assert [r["action"] for r in machine.requests("1111-2222")] == ["mount"]


async def test_a_mount_is_answered_once_the_helper_carried_it_out(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()

    result = await _with_helper(machine, machine.devices.mount("1111-2222"))

    assert (result.id, result.state.value, result.error) == ("1111-2222", "mounted", None)
    assert machine.device("1111-2222").state.value == "mounted"


async def test_a_mount_the_helper_refused_says_what_to_check_with_its_words_as_details(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()

    result = await _with_helper(machine, machine.devices.mount("1111-2222"), ok=False, error="No such filesystem")

    assert (result.state.value, result.error) == ("failed", DRIVE_FAILED)
    spare = machine.device("1111-2222")
    assert (spare.error, spare.error_detail) == (DRIVE_FAILED, "No such filesystem")


@pytest.mark.parametrize(
    "said, told",
    [
        (
            "mount.nfs4: access denied by server while mounting 192.168.1.20:/volume1/video",
            "DS920 does not let this streamer in. Add 192.168.1.50 to the share's NFS exports.",
        ),
        (
            "mount.nfs4: mounting 192.168.1.20:/volume1/video failed, reason given by server: No such file or directory",
            "DS920 has no share at /volume1/video.",
        ),
        (
            "mount.nfs4: Protocol not supported",
            "DS920 does not offer NFS 4. Choose another NFS version for it.",
        ),
        ("mount.nfs4: Connection timed out", "DS920 did not answer. Check that it is on and shares over NFS."),
        ("mount.nfs4: Operation not permitted", SHARE_FAILED),
    ],
)
async def test_a_share_that_would_not_mount_says_what_to_do(tmp_path, said, told):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video", server="DS920")]
    video = nfs_id("192.168.1.20", "/volume1/video")

    result = await _with_helper(machine, machine.devices.mount(video), ok=False, error=said)

    assert (result.state.value, result.error, result.error_detail) == ("failed", told, said)
    assert (machine.device(video).error, machine.device(video).error_detail) == (told, said)
    assert machine.saved == []


async def test_a_mount_the_helper_is_slow_with_is_answered_as_under_way(machine):
    result = await machine.devices.mount("1111-2222")

    assert result.state.value == "mounting"


async def test_an_unmount_the_helper_refused_is_refused_with_its_reason(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()

    with pytest.raises(DeviceError) as refused:
        await _with_helper(machine, machine.devices.unmount(MUSIC), ok=False, error="target is busy")

    assert (refused.value.status, refused.value.message) == (502, "target is busy")


async def test_an_unmount_is_answered_once_carried_out(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()

    result = await _with_helper(machine, machine.devices.unmount(MUSIC))

    assert result.state.value == "unmounted"
    assert machine.device(MUSIC).state.value == "unmounted"


async def test_without_the_mount_helper_nothing_offers_to_mount(machine):
    machine.requests_dir.rmdir()
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video")]

    listing = machine.devices.listing()

    assert listing.mounting_available is False
    assert [d.id for d in listing.devices if d.can_mount] == []
    for action in (machine.devices.mount("1111-2222"), machine.devices.add_share(NewShare(host="nas2", export="/flac"))):
        with pytest.raises(DeviceError) as refused:
            await action
        assert (refused.value.status, refused.value.message) == (503, NO_HELPER)
    assert machine.saved == []


async def test_a_mount_that_cannot_be_asked_for_fails_with_the_reason(machine, monkeypatch):
    def refused(*_args, **_kwargs):
        raise PermissionError("requests")

    monkeypatch.setattr(requests_mod, "open", refused, raising=False)

    result = await machine.devices.mount("1111-2222")

    assert (result.state.value, result.error) == ("failed", UNSENT)
    spare = machine.device("1111-2222")
    assert (spare.state.value, spare.error_detail) == ("failed", None)


async def test_the_switch_on_a_usb_drive_keeps_it_for_the_helper(tmp_path):
    machine = await _Machine(tmp_path, config=StorageConfig(automount_usb=False)).ready()

    await machine.devices.update("3A1F-9C2B", DeviceChanges(at_start=True))

    (record,) = machine.config.mounts
    assert (record.id, record.fs_uuid, record.usb, record.at_start) == ("3A1F-9C2B", "3A1F-9C2B", True, True)
    assert (tmp_path / "state" / "mount-when-present").read_text() == "3A1F-9C2B\n"
    # Switching it on mounts nothing, and a drive mounted already stays so.
    assert machine.requests("3A1F-9C2B") == []


async def test_a_found_share_whose_settings_are_saved_is_kept_as_listed_and_not_mounted(machine):
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video")]
    video = nfs_id("192.168.1.20", "/volume1/video")

    await machine.devices.update(video, DeviceChanges(label="Films", read_only=False))

    record = next(r for r in machine.config.mounts if r.id == video)
    assert (record.label, record.read_only, record.at_start) == ("Films", False, True)
    assert machine.requests(video) == []
    assert machine.device(video).saved


async def test_a_share_mounted_with_settings_is_kept_and_mounted_as_they_say(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    machine.finder.found = [Export(host="192.168.1.20", path="/volume1/video")]
    video = nfs_id("192.168.1.20", "/volume1/video")

    await _with_helper(
        machine,
        machine.devices.mount(video, DeviceChanges(label=" Films ", at_start=False, read_only=False, version=NfsVersion.v3)),
    )

    record = next(r for r in machine.config.mounts if r.id == video)
    assert (record.label, record.at_start, record.read_only, record.version) == ("Films", False, False, NfsVersion.v3)
    assert machine.requests(video) == [
        {
            "action": "mount",
            "fstype": "nfs",
            "source": "192.168.1.20:/volume1/video",
            "options": ["rw", "soft", "timeo=100", "vers=3"],
        }
    ]


async def test_a_usb_drive_mounted_with_its_switch_on_is_kept(tmp_path):
    machine = await _Machine(tmp_path, config=StorageConfig(automount_usb=False), answer_wait=5).ready()

    await _with_helper(machine, machine.devices.mount("9999-0000", DeviceChanges(at_start=True)))

    (record,) = machine.config.mounts
    assert (record.id, record.usb, record.at_start) == ("9999-0000", True, True)


async def test_switching_off_or_renaming_neither_mounts_nor_unmounts(machine):
    await machine.devices.update(MUSIC, DeviceChanges(at_start=False, label="  Records "))

    (record,) = machine.config.mounts
    assert (record.at_start, record.label) == (False, "Records")
    assert machine.requests(MUSIC) == []


async def test_a_share_set_to_another_version_is_mounted_again(machine):
    await machine.devices.update(MUSIC, DeviceChanges(version=NfsVersion.v3))

    assert [r["action"] for r in machine.requests(MUSIC)] == ["unmount", "mount"]
    assert machine.requests(MUSIC)[1]["fstype"] == "nfs"


async def test_forgetting_a_share_unmounts_it_and_takes_its_record_out(machine):
    await machine.devices.forget(MUSIC)

    assert machine.config.mounts == []
    assert machine.requests(MUSIC) == [{"action": "unmount", "eject": False}]


async def test_a_share_typed_in_is_kept_and_mounted_as_asked(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    share = NewShare(host=" 192.168.1.30 ", export="/flac", version=NfsVersion.v3, at_start=False, read_only=False)

    device_id = (await _with_helper(machine, machine.devices.add_share(share))).id

    record = next(r for r in machine.config.mounts if r.id == device_id)
    assert device_id == nfs_id("192.168.1.30", "/flac")
    assert (record.host, record.export, record.version, record.label) == ("192.168.1.30", "/flac", NfsVersion.v3, "flac")
    assert (record.at_start, record.read_only) == (False, False)
    assert machine.requests(device_id) == [
        {"action": "mount", "fstype": "nfs", "source": "192.168.1.30:/flac", "options": ["rw", "soft", "timeo=100", "vers=3"]}
    ]


async def test_a_share_typed_in_that_failed_is_listed_not_kept_until_put_away(tmp_path):
    machine = await _Machine(tmp_path, answer_wait=5).ready()
    flac = nfs_id("nas2", "/flac")

    result = await _with_helper(
        machine, machine.devices.add_share(NewShare(host="nas2", export="/flac")), ok=False, error="mount.nfs4: Connection timed out"
    )

    assert (result.state.value, result.error) == ("failed", "nas2 did not answer. Check that it is on and shares over NFS.")
    assert machine.saved == []
    typed = machine.device(flac)
    assert (typed.medium.value, typed.label, typed.summary, typed.saved) == ("network", "flac", "NFS · nas2", False)
    await machine.devices.dismiss(flac)
    assert flac not in machine.ids()


async def test_a_share_saved_while_it_mounts_keeps_what_was_saved(machine):
    flac = nfs_id("nas2", "/flac")
    assert (await machine.devices.add_share(NewShare(host="nas2", export="/flac"))).state.value == "mounting"

    await machine.devices.update(flac, DeviceChanges(label="Lossless", at_start=False))
    async def mounted():
        while machine.device(flac).state.value != "mounted":
            await machine.manager.refresh()
            await asyncio.sleep(0.01)

    await _with_helper(machine, asyncio.wait_for(mounted(), 2))
    await asyncio.sleep(0.05)

    record = next(r for r in machine.config.mounts if r.id == flac)
    assert (record.label, record.at_start) == ("Lossless", False)


async def test_a_share_typed_in_that_is_kept_already_is_kept_as_asked_now(tmp_path):
    flac = nfs_id("nas.local", "/flac")
    machine = await _Machine(
        tmp_path, config=StorageConfig(mounts=[NfsMount(id=flac, label="Lossless", host="nas.local", export="/flac")])
    ).ready()

    await machine.devices.add_share(NewShare(host="nas.local", export="/flac", at_start=False))

    (record,) = machine.config.mounts
    assert (record.label, record.at_start, record.version) == ("Lossless", False, NfsVersion.v4_2)


@pytest.mark.parametrize(
    "action, status",
    [
        (lambda d: d.mount("internal"), 409),
        (lambda d: d.mount("aaaa-bbbb"), 409),
        (lambda d: d.mount("3A1F-9C2B"), 409),
        (lambda d: d.unmount("1111-2222"), 409),
        (lambda d: d.forget("1111-2222"), 409),
        (lambda d: d.update("internal", DeviceChanges(label="x")), 409),
        (lambda d: d.update("3A1F-9C2B", DeviceChanges(at_start=True)), 409),
        (lambda d: d.update("1111-2222", DeviceChanges(version=NfsVersion.v3)), 422),
        (lambda d: d.dismiss("1111-2222"), 409),
        (lambda d: d.mount("1111-2222", DeviceChanges(version=NfsVersion.v3)), 422),
        (lambda d: d.mount("nope"), 404),
        (lambda d: d.add_share(NewShare(host="nas;rm", export="/music")), 422),
        (lambda d: d.add_share(NewShare(host="nas.local", export="music")), 422),
        (lambda d: d.add_share(NewShare(host="nas.local", export="/volume1/music")), 409),
    ],
)
async def test_what_does_not_apply_is_refused(machine, action, status):
    with pytest.raises(DeviceError) as refused:
        await action(machine.devices)

    assert refused.value.status == status
    assert machine.saved == []


async def test_a_change_the_settings_refuse_is_refused_with_their_reason(machine):
    machine.refused = "No"

    with pytest.raises(DeviceError) as refused:
        await machine.devices.update("1111-2222", DeviceChanges(label="Spare"))

    assert (refused.value.status, refused.value.message) == (422, "No")
    assert machine.requests("1111-2222") == []


@pytest.fixture
async def client(machine):
    app = FastAPI()
    register_device_routes(app, machine.devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_the_routes_answer_the_list_and_carry_the_actions_out(client, machine):
    listing = (await client.get("/server/storage/devices")).json()
    mounted = await client.post("/server/storage/devices/1111-2222/mount", json={"label": "Spare"})
    renamed = await client.put(f"/server/storage/devices/{MUSIC}", json={"label": "Records"})
    unmounted = await client.post(f"/server/storage/devices/{MUSIC}/unmount")
    forgotten = await client.delete(f"/server/storage/devices/{MUSIC}")

    assert [d["id"] for d in listing["devices"]][0] == "internal"
    assert listing["devices"][0]["can_mount"] is False and listing["scan"]["running"] is False
    assert [r.status_code for r in (mounted, renamed, unmounted, forgotten)] == [200, 200, 200, 200]
    assert mounted.json() == {"id": "1111-2222", "state": "mounting", "error": None, "error_detail": None}
    assert unmounted.json() == {"id": MUSIC, "state": "mounted", "error": None, "error_detail": None}
    assert machine.config.mounts == []
    # Not kept until it mounts, so there is nothing to forget, nor a failure to put away.
    assert (await client.delete("/server/storage/devices/1111-2222")).status_code == 409
    assert (await client.delete("/server/storage/devices/1111-2222/failure")).status_code == 409


async def test_the_routes_refuse_with_the_reason(client):
    assert (await client.post("/server/storage/devices/nope/mount")).status_code == 404
    refused = await client.post("/server/storage/devices/internal/unmount")
    assert refused.status_code == 409 and refused.json()["detail"]


async def test_a_host_is_asked_for_its_exports_and_a_share_kept_by_its_path(client, machine):
    probed = (await client.post("/server/storage/shares/probe", json={"host": "nas2"})).json()
    added = await client.post("/server/storage/shares", json={"host": "nas2", "export": "/flac"})

    assert probed == {
        "host": "nas2",
        "reachable": True,
        "error": None,
        "exports": [{"id": nfs_id("nas2", "/flac"), "host": "nas2", "export": "/flac", "server": None, "clients": []}],
    }
    assert added.status_code == 200
    assert added.json() == {"id": nfs_id("nas2", "/flac"), "state": "mounting", "error": None, "error_detail": None}
    assert (await client.post("/server/storage/shares/probe", json={"host": "a b"})).status_code == 422


async def test_the_search_for_shares_starts_over_when_asked(client, machine):
    assert (await client.post("/server/storage/shares/scan")).status_code == 200
    assert machine.finder.rescans == 1


def test_the_address_a_host_sees_this_machine_by_is_the_one_toward_it():
    assert address_seen_by("127.0.0.1") == "127.0.0.1"
    assert address_seen_by("not a host") is None
