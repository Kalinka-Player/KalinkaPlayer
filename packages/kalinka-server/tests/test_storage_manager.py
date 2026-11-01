"""The volumes the storage manager lists, and the mounts it asks for."""

import json

import pytest
from kalinka_plugin_sdk.mounts import list_mounts
from kalinka_plugin_sdk.storage import (
    LocationStatus,
    StorageLocation,
    VolumeKind,
    VolumeState,
)

from kalinka_server.config_model import DiskMount, NfsMount, StorageConfig
from kalinka_server.storage.manager import Adopter, StorageManager
from kalinka_server.storage.requests import MountRequests

MOUNTINFO = "\n".join(
    [
        "1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw",
        "50 1 8:1 / /media/kalinka/3A1F-9C2B ro,nosuid - vfat /dev/sda1 ro",
        "51 1 8:17 / /mnt/archive ro - ext4 /dev/sdb1 ro",
        "52 1 0:60 / /media/kalinka/rec_nas ro - nfs4 nas.local:/export/music ro",
        "53 1 8:17 /films /mnt/films ro - ext4 /dev/sdb1 ro",
        "54 1 0:70 / /run/media/me/scratch rw - tmpfs tmpfs rw",
    ]
)


def _udev(tmp_path):
    data = tmp_path / "udev"
    data.mkdir()
    (data / "b8:1").write_text(
        "E:ID_FS_LABEL=SANDISK\nE:ID_FS_UUID=3A1F-9C2B\nE:ID_FS_TYPE=vfat\n"
        "E:ID_FS_USAGE=filesystem\nE:ID_BUS=usb\nE:ID_VENDOR=SanDisk\n"
    )
    (data / "b8:17").write_text(
        "E:ID_FS_LABEL=Archive\nE:ID_FS_UUID=aaaa-bbbb\nE:ID_FS_TYPE=ext4\n"
        "E:ID_FS_USAGE=filesystem\nE:ID_BUS=ata\n"
    )
    (data / "b8:33").write_text(
        "E:ID_FS_LABEL=Spare\nE:ID_FS_UUID=1111-2222\nE:ID_FS_TYPE=ext4\n"
        "E:ID_FS_USAGE=filesystem\nE:ID_BUS=ata\n"
    )
    (data / "b8:49").write_text(
        "E:ID_FS_UUID=9999-0000\nE:ID_FS_TYPE=vfat\nE:ID_FS_USAGE=filesystem\nE:ID_BUS=usb\n"
    )
    return str(data)


class _Box:
    def __init__(self, tmp_path, config=None, mountinfo=MOUNTINFO, adopters=()):
        self.config = config or StorageConfig(
            mounts=[
                NfsMount(id="rec_nas", label="nas/music", host="nas.local", export="/export/music"),
                DiskMount(id="rec_disk", label="Spare", fs_uuid="1111-2222"),
            ]
        )
        self.events = []
        self.media = tmp_path / "music"
        self.media.mkdir()
        self.requests_dir = tmp_path / "requests"
        self.requests_dir.mkdir()
        self.results_dir = tmp_path / "results"
        self.results_dir.mkdir()
        self.marker = tmp_path / "state" / "no-automount"
        self.mountinfo = mountinfo
        self.space = (31_000_000_000, 12_000_000_000)
        self.manager = StorageManager(
            config=lambda: self.config,
            media_dir=str(self.media),
            dispatch=self.events.append,
            adopters=lambda: adopters,
            requests=MountRequests(str(self.requests_dir), str(self.results_dir)),
            mountinfo=lambda: list_mounts(self.mountinfo),
            udev_data_dir=_udev(tmp_path),
            marker=str(self.marker),
            statvfs=lambda _p: self.space,
            fstab=str(tmp_path / "fstab"),
            sys_dev_block=str(tmp_path / "sysdev"),
        )

    def _latest(self, volume_id):
        return json.loads(sorted(self.requests_dir.glob(f"*.{volume_id}.json"))[-1].read_text())

    def request(self, volume_id):
        """The latest request for ``volume_id``, as the helper reads it."""
        asked = self._latest(volume_id)
        del asked["seq"]
        return asked

    def answer(self, volume_id, ok, error=None, at=None):
        import time

        (self.results_dir / f"{volume_id}.json").write_text(
            json.dumps(
                {"ok": ok, "error": error, "seq": self._latest(volume_id)["seq"], "at": at or time.time() + 1}
            )
        )


def test_volumes_are_the_music_folder_drives_and_mounts(tmp_path):
    box = _Box(tmp_path)

    volumes = box.manager.snapshot()

    assert [(v.id, v.kind, v.label, v.state) for v in volumes] == [
        ("internal", VolumeKind.INTERNAL, "Internal storage", VolumeState.MOUNTED),
        ("3A1F-9C2B", VolumeKind.REMOVABLE, "SANDISK", VolumeState.MOUNTED),
        ("aaaa-bbbb", VolumeKind.MOUNT, "Archive", VolumeState.MOUNTED),
        ("rec_nas", VolumeKind.MOUNT, "nas/music", VolumeState.MOUNTED),
        ("rec_disk", VolumeKind.MOUNT, "Spare", VolumeState.UNMOUNTED),
    ]
    stick = volumes[1]
    assert (stick.fs_type, stick.device, stick.bus, stick.size_bytes) == (
        "vfat", "/dev/sda1", "usb", 31_000_000_000,
    )


def test_a_bind_view_and_a_memory_filesystem_are_not_volumes(tmp_path):
    points = {v.mount_point for v in _Box(tmp_path).manager.snapshot()}

    assert "/mnt/films" not in points and "/run/media/me/scratch" not in points


def test_two_sticks_with_one_uuid_are_two_volumes(tmp_path):
    box = _Box(
        tmp_path,
        config=StorageConfig(),
        mountinfo="\n".join(
            [
                "1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw",
                "60 1 8:49 / /run/media/me/9999-0000 rw - vfat /dev/sdd1 rw",
                "61 1 8:65 / /run/media/me/9999-00001 rw - vfat /dev/sde1 rw",
                "62 1 8:49 / /mnt/again rw - vfat /dev/sdd1 rw",
            ]
        ),
    )
    (tmp_path / "udev" / "b8:65").write_text(
        "E:ID_FS_UUID=9999-0000\nE:ID_FS_TYPE=vfat\nE:ID_FS_USAGE=filesystem\nE:ID_BUS=usb\n"
    )

    drives = {v.id: v.mount_point for v in box.manager.snapshot() if v.kind != VolumeKind.INTERNAL}

    # The second is told apart by its device; the first, mounted twice, is one.
    assert drives == {
        "9999-0000": "/run/media/me/9999-0000",
        "9999-0000-sde1": "/run/media/me/9999-00001",
    }


def test_a_mount_waiting_on_the_helper_is_mounting(tmp_path):
    box = _Box(tmp_path)
    box.manager.mount("rec_disk")

    assert box.manager.snapshot()[-1].state == VolumeState.MOUNTING
    assert box.request("rec_disk") == {
        "id": "rec_disk", "action": "mount", "fstype": "disk",
        "source": "UUID=1111-2222", "options": ["ro"],
    }


def test_a_mount_the_helper_refused_is_failed_with_its_reason(tmp_path):
    box = _Box(tmp_path)
    box.manager.mount("rec_disk")
    box.answer("rec_disk", ok=False, error="no filesystem with UUID 1111-2222")

    volume = box.manager.snapshot()[-1]
    assert (volume.state, volume.error) == (VolumeState.FAILED, "no filesystem with UUID 1111-2222")


async def test_a_change_reaches_subscribers_then_clients_then_listeners(tmp_path):
    box = _Box(tmp_path)
    order = []
    box.manager.subscribe(lambda volumes: order.append(("subscriber", len(volumes))))

    async def listener():
        order.append(("listener", len(box.events)))

    box.manager.after_change(listener)

    await box.manager.refresh()
    await box.manager.refresh()

    assert order == [("subscriber", 5), ("listener", 1)]
    assert len(box.events) == 1 and len(box.events[0].volumes) == 5


class _Library:
    """A library holding the volume ``aaaa-bbbb``."""

    async def location_status(self, location: StorageLocation):
        return LocationStatus(holds=location.volume_id == "aaaa-bbbb")

    async def adopt_location(self, location):
        return None

    async def release_location(self, location):
        return None


async def test_a_library_says_what_it_holds_of_each_volume(tmp_path):
    box = _Box(tmp_path, adopters=[Adopter("localfiles", "My Library", _Library())])
    await box.manager.refresh()

    libraries = {v.id: v.library for v in box.manager.volumes()}
    assert libraries["aaaa-bbbb"].holds and not libraries["3A1F-9C2B"].holds
    assert libraries["rec_disk"] is None
    assert await box.manager.library_holding("/mnt/archive/Jazz") == "My Library"
    assert await box.manager.library_holding("/media/kalinka/3A1F-9C2B/Jazz") is None


async def test_each_volume_is_summed_up_for_clients(tmp_path):
    box = _Box(tmp_path)
    await box.manager.refresh()

    summaries = {v.id: v.summary for v in box.manager.volumes()}
    assert summaries["3A1F-9C2B"].startswith("USB drive")
    assert summaries["rec_disk"] == "Disk"


async def test_a_path_is_located_on_the_deepest_volume(tmp_path):
    box = _Box(tmp_path)
    await box.manager.refresh()

    located = box.manager.location_of("/media/kalinka/3A1F-9C2B/Jazz/Kind of Blue")

    assert (located.volume_id, located.subpath, located.label) == ("3A1F-9C2B", "Jazz/Kind of Blue", "SANDISK")
    assert box.manager.location_of("/etc/passwd") is None


@pytest.mark.parametrize("enabled", [False, True])
def test_the_automount_switch_is_left_where_the_helper_reads_it(tmp_path, enabled):
    box = _Box(tmp_path, config=StorageConfig(automount_usb=enabled))
    box.marker.parent.mkdir(parents=True)
    if enabled:
        box.marker.write_text("")

    box.manager._apply_automount()

    assert box.marker.exists() is not enabled


async def test_saved_mounts_are_mounted_and_removed_ones_unmounted(tmp_path):
    box = _Box(tmp_path, config=StorageConfig())
    await box.manager.start()
    await box.manager.stop()
    assert list(box.requests_dir.iterdir()) == []

    box.config = StorageConfig(mounts=[NfsMount(id="rec_nas", host="nas.local", export="/export/music")])
    await box.manager.config_changed()
    assert box.request("rec_nas")["options"] == ["ro", "soft", "timeo=100", "vers=4.2"]
    assert box.request("rec_nas")["fstype"] == "nfs4"

    box.config = StorageConfig()
    await box.manager.config_changed()
    assert box.request("rec_nas") == {"id": "rec_nas", "action": "unmount", "eject": False}


async def test_only_what_kalinka_mounted_can_be_ejected(tmp_path):
    box = _Box(tmp_path)
    await box.manager.refresh()

    assert box.manager.eject("3A1F-9C2B")
    assert box.request("3A1F-9C2B")["eject"] is True
    assert not box.manager.eject("internal")
    assert not box.manager.eject("aaaa-bbbb")
    assert not box.manager.eject("nope")


def test_the_disk_picker_offers_unmounted_internal_filesystems(tmp_path):
    offered = [uuid for uuid, _ in _Box(tmp_path).manager.unmounted_filesystems()]

    assert offered == ["1111-2222"]


def test_the_disk_picker_leaves_out_what_fstab_places(tmp_path):
    box = _Box(tmp_path)
    (tmp_path / "fstab").write_text('UUID="1111-2222" /srv/spare ext4 noauto 0 0\n')

    assert box.manager.unmounted_filesystems() == []


def test_the_disk_picker_leaves_out_the_disk_the_system_uses(tmp_path):
    """Spare (8:33) shares a disk with Archive, which the system mounted."""
    box = _Box(tmp_path)
    disk = tmp_path / "sys/block/sdb"
    links = tmp_path / "sysdev"
    links.mkdir()
    for numbers, part in (("8:17", "sdb1"), ("8:33", "sdb3")):
        (disk / part).mkdir(parents=True)
        (disk / part / "partition").write_text("1")
        (links / numbers).symlink_to(disk / part)

    assert box.manager.unmounted_filesystems() == []


async def test_free_space_moving_is_no_news(tmp_path):
    box = _Box(tmp_path)
    await box.manager.refresh()
    box.space = (31_000_000_000, 11_000_000_000)

    await box.manager.refresh()

    assert len(box.events) == 1
    assert box.manager.volume("internal").free_bytes == 11_000_000_000


async def test_a_settings_change_reaches_the_listeners_with_no_volume_changed(tmp_path):
    """Switching Drives off changes no volume, but what the server offers."""
    box = _Box(tmp_path, config=StorageConfig())
    await box.manager.refresh()
    told = []

    async def listener():
        told.append(True)

    box.manager.after_change(listener)
    await box.manager.config_changed()

    assert told == [True] and len(box.events) == 1
