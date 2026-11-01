"""The root helper that mounts storage for Kalinka.

It runs as root on a real box, so every path it touches is redirected into
``tmp_path`` and every command it runs is recorded instead of run.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "media-mount"


def _load():
    loader = importlib.machinery.SourceFileLoader("media_mount", str(HELPER))
    spec = importlib.util.spec_from_loader("media_mount", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Box:
    """A machine for the helper: its files under tmp_path, its commands faked."""

    def __init__(self, module, tmp_path, monkeypatch):
        self.module = module
        self.root = tmp_path
        self.calls: list[list[str]] = []
        self.udev: dict[str, dict[str, str]] = {}
        self.mount_lines: list[str] = []
        self.failing: set[str] = set()
        self.failing_once: set[str] = set()
        self.root_line = "1 0 8:2 / / rw - ext4 /dev/sda2 rw"
        self._seq = 0
        for name, rel in {
            "MEDIA_ROOT": "media/kalinka",
            "REQUESTS_DIR": "run/requests",
            "RESULTS_DIR": "run/results",
            "NO_AUTOMOUNT_MARKER": "state/no-automount",
            "FSTAB": "etc/fstab",
            "MOUNTINFO": "proc/mountinfo",
            "SWAPS": "proc/swaps",
            "DEV_BY_UUID": "dev/disk/by-uuid",
            "SYS_CLASS_BLOCK": "sys/class/block",
            "SYS_DEV_BLOCK": "sys/dev/block",
        }.items():
            monkeypatch.setattr(module, name, str(tmp_path / rel))
        (tmp_path / "run/requests").mkdir(parents=True)
        (tmp_path / "etc").mkdir()
        (tmp_path / "proc").mkdir()
        self.media_root = str(tmp_path / "media/kalinka")
        monkeypatch.setattr(module, "_service_ids", lambda: (998, 997))
        monkeypatch.setattr(module.subprocess, "run", self._run)
        self._write_mountinfo()

    def _run(self, command, **_kwargs):
        self.calls.append(command)
        if command[0] == "udevadm":
            name = command[-1].split("=", 1)[1]
            props = self.udev.get(name, {})
            out = "\n".join(f"{k}={v}" for k, v in props.items())
            return subprocess.CompletedProcess(command, 0, out, "")
        if command[0] in self.failing or command[0] in self.failing_once:
            self.failing_once.discard(command[0])
            return subprocess.CompletedProcess(command, 1, "", "it broke")
        if command[0] == "systemd-umount":
            self.mount_lines = [l for l in self.mount_lines if f" {command[-1]} " not in l]
            self._write_mountinfo()
        return subprocess.CompletedProcess(command, 0, "", "")

    def mounted(self, source, point, fstype="ext4"):
        n = len(self.mount_lines) + 30
        self.mount_lines.append(f"{n} 1 8:{n} / {point} rw - {fstype} {source} rw")
        self._write_mountinfo()

    def _write_mountinfo(self):
        lines = [self.root_line, *self.mount_lines]
        (self.root / "proc/mountinfo").write_text("\n".join(lines) + "\n")

    def fstab(self, text):
        (self.root / "etc/fstab").write_text(text)

    def request(self, request_id, **body):
        self._seq += 1
        seq = f"{self._seq:020d}"
        path = self.root / "run/requests" / f"{seq}.{request_id}.json"
        path.write_text(json.dumps({"id": request_id, "seq": seq, **body}))
        return path

    def boot_from(self, disk, partition, numbers="8:2"):
        """The root on ``partition``, mounted by the kernel as /dev/root."""
        self.root_line = f"1 0 {numbers} / / rw - ext4 /dev/root rw"
        links = self.root / "sys/dev/block"
        links.mkdir(parents=True, exist_ok=True)
        (links / numbers).symlink_to(self.root / "sys/devices/usb" / disk / partition)
        self._write_mountinfo()

    def result(self, request_id):
        return json.loads((self.root / "run/results" / f"{request_id}.json").read_text())

    def commands(self, name):
        return [c for c in self.calls if c[0] == name]

    def disk(self, disk, *partitions, bus="usb"):
        """A disk in /sys/class/block with its partitions and a delete knob."""
        devices = self.root / "sys/devices" / bus / disk
        (devices / "device").mkdir(parents=True)
        (devices / "device/delete").write_text("")
        block = self.root / "sys/class/block"
        block.mkdir(parents=True, exist_ok=True)
        (block / disk).symlink_to(devices)
        for part in partitions:
            (devices / part).mkdir()
            (devices / part / "partition").write_text("1")
            (block / part).symlink_to(devices / part)
        return devices / "device/delete"


@pytest.fixture
def box(tmp_path, monkeypatch):
    return _Box(_load(), tmp_path, monkeypatch)


USB_VFAT = {
    "ID_FS_USAGE": "filesystem",
    "ID_FS_TYPE": "vfat",
    "ID_FS_UUID": "3A1F-9C2B",
    "ID_FS_LABEL": "SANDISK",
    "ID_BUS": "usb",
}


def _mount_command(box):
    (command,) = box.commands("systemd-mount")
    options = next(a for a in command if a.startswith("--options="))
    return command, options[len("--options="):].split(",")


def test_a_usb_stick_is_mounted_read_only_by_its_uuid(box):
    box.udev["/dev/sdb1"] = USB_VFAT

    assert box.module.mount_device("/dev/sdb1") == 0

    command, options = _mount_command(box)
    assert command[-3:] == ["--", "/dev/sdb1", f"{box.media_root}/3A1F-9C2B"]
    assert "--type=vfat" in command and "--no-block" in command
    assert "--property=LazyUnmount=yes" in command
    assert options[:4] == ["ro", "nosuid", "nodev", "noexec"]
    assert {"uid=998", "gid=997", "umask=022", "utf8"} <= set(options)


def test_ntfs_is_mounted_with_the_kernel_driver(box):
    box.udev["/dev/sdb1"] = {**USB_VFAT, "ID_FS_TYPE": "ntfs", "ID_FS_UUID": "5E8F1C2A3B4D5E6F"}

    box.module.mount_device("/dev/sdb1")

    command, options = _mount_command(box)
    assert "--type=ntfs3" in command
    assert "uid=998" in options and "utf8" not in options


def test_a_filesystem_with_owners_keeps_them(box):
    box.udev["/dev/sdb1"] = {**USB_VFAT, "ID_FS_TYPE": "ext4"}

    box.module.mount_device("/dev/sdb1")

    _, options = _mount_command(box)
    assert options == ["ro", "nosuid", "nodev", "noexec"]


def test_a_stick_without_a_uuid_is_named_after_its_device(box):
    box.udev["/dev/sdb"] = {**USB_VFAT, "ID_FS_UUID": ""}

    box.module.mount_device("/dev/sdb")

    command, _ = _mount_command(box)
    assert command[-1] == f"{box.media_root}/sdb"


def test_a_second_stick_with_the_same_uuid_is_named_after_its_device_too(box):
    box.udev["/dev/sdc1"] = USB_VFAT
    box.mounted("/dev/sdb1", f"{box.media_root}/3A1F-9C2B", "vfat")

    box.module.mount_device("/dev/sdc1")

    command, _ = _mount_command(box)
    assert command[-1] == f"{box.media_root}/3A1F-9C2B-sdc1"


@pytest.mark.parametrize(
    "props",
    [
        {**USB_VFAT, "ID_FS_USAGE": "other"},
        {**USB_VFAT, "ID_FS_TYPE": "swap"},
        {**USB_VFAT, "ID_FS_TYPE": "crypto_LUKS"},
    ],
)
def test_what_is_not_a_known_filesystem_is_left_alone(box, props):
    box.udev["/dev/sdb1"] = props

    box.module.mount_device("/dev/sdb1")

    assert box.commands("systemd-mount") == []


def test_a_device_already_mounted_is_left_alone(box):
    """A Pi booted from USB has its own root on a USB disk."""
    box.udev["/dev/sda2"] = {**USB_VFAT, "ID_FS_TYPE": "ext4"}

    box.module.mount_device("/dev/sda2")

    assert box.commands("systemd-mount") == []


@pytest.mark.parametrize(
    "line",
    [
        "UUID=3A1F-9C2B /mnt/music auto nofail,ro 0 0",
        'UUID="3A1F-9C2B" /mnt/music auto nofail,ro 0 0',
        "LABEL=SANDISK /mnt/music auto nofail 0 0",
        "/dev/sdb1 /mnt/music vfat defaults 0 0",
    ],
)
def test_a_device_the_owner_put_in_fstab_is_left_alone(box, line):
    box.fstab(f"# the owner's\n{line}\n")
    box.udev["/dev/sdb1"] = USB_VFAT

    box.module.mount_device("/dev/sdb1")

    assert box.commands("systemd-mount") == []


def test_a_commented_fstab_line_does_not_count(box):
    box.fstab("#UUID=3A1F-9C2B /mnt/music auto nofail 0 0\n")
    box.udev["/dev/sdb1"] = USB_VFAT

    box.module.mount_device("/dev/sdb1")

    assert len(box.commands("systemd-mount")) == 1


def test_automount_switched_off_mounts_nothing(box):
    marker = Path(box.module.NO_AUTOMOUNT_MARKER)
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    box.udev["/dev/sdb1"] = USB_VFAT

    box.module.mount_device("/dev/sdb1")

    assert box.calls == []


def test_an_nfs_share_is_mounted_on_request(box):
    path = box.request(
        "rec_nas",
        action="mount",
        fstype="nfs4",
        source="nas.local:/export/music",
        options=["ro", "soft", "timeo=100", "vers=4.2"],
    )

    box.module.apply_requests()

    command, options = _mount_command(box)
    assert command[-3:] == ["--", "nas.local:/export/music", f"{box.media_root}/rec_nas"]
    assert "--type=nfs4" in command and "--no-block" not in command
    assert options == ["ro", "soft", "timeo=100", "vers=4.2", "retry=1", "nosuid", "nodev", "noexec"]
    assert box.result("rec_nas")["ok"] is True
    assert not path.exists()


def test_a_disk_is_mounted_by_uuid_as_udev_knows_it(box):
    by_uuid = Path(box.module.DEV_BY_UUID)
    by_uuid.mkdir(parents=True)
    (by_uuid / "1111-2222").write_text("")
    box.udev[str(by_uuid / "1111-2222")] = {**USB_VFAT, "ID_FS_TYPE": "exfat"}

    box.request("rec_disk", action="mount", fstype="disk", source="UUID=1111-2222")
    box.module.apply_requests()

    command, options = _mount_command(box)
    assert "--type=exfat" in command and "ro" in options and "uid=998" in options
    assert box.result("rec_disk")["ok"] is True


@pytest.mark.parametrize(
    "body, reason",
    [
        ({"action": "mount", "fstype": "cifs", "source": "//nas/music"}, "not allowed"),
        ({"action": "mount", "fstype": "nfs4", "source": "-oexec:/x"}, "host:/path"),
        ({"action": "mount", "fstype": "nfs4", "source": "nas:/x,exec"}, "invalid share"),
        ({"action": "mount", "fstype": "nfs4", "source": "nas:/x", "options": ["exec"]}, "not allowed"),
        ({"action": "mount", "fstype": "nfs4", "source": "nas:/x", "options": ["timeo=0"]}, "out of range"),
        ({"action": "mount", "fstype": "disk", "source": "/dev/sda2"}, "UUID="),
        ({"action": "format"}, "unknown action"),
    ],
)
def test_requests_outside_the_allowlist_are_refused(box, body, reason):
    box.request("rec_bad", **body)

    box.module.apply_requests()

    result = box.result("rec_bad")
    assert result["ok"] is False and reason in result["error"]
    assert box.commands("systemd-mount") == []


def test_a_request_cannot_name_a_target_outside_the_media_root(box):
    seq = "00000000000000000001"
    path = Path(box.module.REQUESTS_DIR) / f"{seq}.x.json"
    path.write_text(json.dumps({"id": "../../etc", "seq": seq, "action": "unmount"}))

    box.module.apply_requests()

    assert box.result("x")["ok"] is False
    assert box.commands("systemd-umount") == []


def test_eject_unmounts_every_partition_and_powers_the_disk_off(box):
    delete = box.disk("sdb", "sdb1", "sdb2")
    box.mounted("/dev/sdb1", f"{box.media_root}/3A1F-9C2B", "vfat")
    box.mounted("/dev/sdb2", f"{box.media_root}/7777-8888", "vfat")

    box.request("3A1F-9C2B", action="unmount", eject=True)
    box.module.apply_requests()

    assert [c[-1] for c in box.commands("systemd-umount")] == [
        f"{box.media_root}/3A1F-9C2B",
        f"{box.media_root}/7777-8888",
    ]
    assert delete.read_text() == "1"
    assert box.result("3A1F-9C2B")["ok"] is True


def test_a_failed_mount_is_reported_with_its_reason(box):
    box.failing.add("systemd-mount")
    box.request("rec_nas", action="mount", fstype="nfs4", source="nas:/music")

    box.module.apply_requests()

    result = box.result("rec_nas")
    assert (result["ok"], result["error"], result["action"]) == (False, "it broke", "mount")


def test_a_second_stick_plugged_in_at_boot_with_the_same_uuid_mounts_beside_it(box):
    """Both announced at once, the other took the target before this one."""
    box.udev["/dev/sdc1"] = USB_VFAT
    box.failing_once.add("systemd-mount")

    box.module.mount_device("/dev/sdc1")

    assert [c[-1] for c in box.commands("systemd-mount")] == [
        f"{box.media_root}/3A1F-9C2B",
        f"{box.media_root}/3A1F-9C2B-sdc1",
    ]


def test_the_disk_a_usb_booted_pi_runs_from_is_not_mounted_again(box):
    box.disk("sda", "sda1", "sda2", "sda3")
    box.boot_from("sda", "sda2")
    box.udev["/dev/sda3"] = USB_VFAT

    box.module.mount_device("/dev/sda3")

    assert box.commands("systemd-mount") == []


def test_requests_run_in_the_order_they_were_asked(box):
    """A remount: the old share goes before the new one comes."""
    target = f"{box.media_root}/rec_nas"
    box.mounted("old.local:/music", target, "nfs4")
    box.request("rec_nas", action="unmount")
    box.request("rec_nas", action="mount", fstype="nfs4", source="nas.local:/music")

    box.module.apply_requests()

    assert [c[0] for c in box.calls if c[0].startswith("systemd-")] == ["systemd-umount", "systemd-mount"]
    assert box.result("rec_nas")["seq"] == "00000000000000000002"
    assert list(Path(box.module.REQUESTS_DIR).iterdir()) == []


def test_whatever_is_left_in_the_request_directory_is_taken_out(box, tmp_path):
    requests = Path(box.module.REQUESTS_DIR)
    secret = tmp_path / "secret"
    secret.write_text(json.dumps({"id": "y", "seq": "00000000000000000002", "action": "unmount"}))
    (requests / "00000000000000000001.x.json").mkdir()
    (requests / "00000000000000000002.y.json").symlink_to(secret)
    os.mkfifo(requests / "00000000000000000003.z.json")
    (requests / "00000000000000000004.w.json").write_text("[" * 5000)
    (requests / "notes.txt").write_text("")
    box.request("rec_nas", action="mount", fstype="nfs4", source="nas.local:/music")

    box.module.apply_requests()

    assert [p.name for p in requests.iterdir() if not p.name.startswith(".")] == []
    assert [box.result(i)["ok"] for i in "xyzw"] == [False] * 4
    assert box.result("rec_nas")["ok"] is True
    assert secret.exists()


@pytest.mark.parametrize(
    "props, fstab, reason",
    [
        ({}, "UUID=1111-2222 /srv/music exfat noauto 0 0", "fstab"),
        ({"ID_PART_ENTRY_TYPE": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"}, "", "EFI"),
    ],
)
def test_a_disk_the_system_keeps_for_itself_is_refused(box, props, fstab, reason):
    by_uuid = Path(box.module.DEV_BY_UUID)
    by_uuid.mkdir(parents=True)
    (by_uuid / "1111-2222").write_text("")
    box.udev[str(by_uuid / "1111-2222")] = {**USB_VFAT, "ID_FS_TYPE": "exfat", "ID_FS_UUID": "1111-2222", **props}
    box.fstab(fstab)

    box.request("rec_disk", action="mount", fstype="disk", source="UUID=1111-2222")
    box.module.apply_requests()

    assert reason in box.result("rec_disk")["error"]
    assert box.commands("systemd-mount") == []


def test_a_partition_of_the_system_disk_is_refused(box):
    box.disk("sda", "sda1", "sda2", "sda3")
    box.boot_from("sda", "sda2")
    by_uuid = Path(box.module.DEV_BY_UUID)
    by_uuid.mkdir(parents=True)
    (box.root / "dev/sda3").write_text("")
    (by_uuid / "1111-2222").symlink_to(box.root / "dev/sda3")
    box.udev[str(by_uuid / "1111-2222")] = {**USB_VFAT, "ID_FS_TYPE": "ext4"}

    box.request("rec_disk", action="mount", fstype="disk", source="UUID=1111-2222")
    box.module.apply_requests()

    assert "disk the system runs from" in box.result("rec_disk")["error"]


def test_a_disk_off_usb_is_unmounted_but_never_taken_off_its_bus(box):
    delete = box.disk("sdb", "sdb1", bus="pci")
    box.mounted("/dev/sdb1", f"{box.media_root}/3A1F-9C2B", "vfat")

    box.request("3A1F-9C2B", action="unmount", eject=True)
    box.module.apply_requests()

    assert len(box.commands("systemd-umount")) == 1
    assert delete.read_text() == ""


def test_the_media_root_is_the_one_the_server_watches():
    from kalinka_plugin_sdk.mounts import KALINKA_MEDIA_ROOT

    assert _load().MEDIA_ROOT == KALINKA_MEDIA_ROOT
