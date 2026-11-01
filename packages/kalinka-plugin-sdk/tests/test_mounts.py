"""Reading what is mounted, and what udev says of a disk."""

from kalinka_plugin_sdk.mounts import (
    BlockFacts,
    Mount,
    block_facts,
    covering_mount,
    is_network_fs,
    list_mounts,
)

MOUNTINFO = "\n".join(
    [
        "36 25 0:32 / / rw,relatime shared:1 - ext4 /dev/mmcblk0p2 rw",
        "50 36 8:1 / /media/kalinka/3A1F-9C2B ro,nosuid - vfat /dev/sda1 ro",
        r"51 36 8:17 / /media/My\040Disk ro - exfat /dev/sdb1 ro",
        "52 36 0:55 / /media/kalinka/rec_nas ro - nfs4 nas.local:/export/music ro",
    ]
)


def test_rows_are_read_with_their_device_and_escapes():
    mounts = list_mounts(MOUNTINFO)

    assert mounts[1] == Mount("/media/kalinka/3A1F-9C2B", "vfat", "/dev/sda1", "8:1", "/")
    assert mounts[2].mount_point == "/media/My Disk"


def test_the_deepest_mount_covers_a_path():
    mounts = list_mounts(MOUNTINFO)

    assert covering_mount("/media/kalinka/3A1F-9C2B/Jazz", mounts).source == "/dev/sda1"
    assert covering_mount("/media/kalinka/elsewhere", mounts).mount_point == "/"


def test_nfs_is_a_network_filesystem():
    assert is_network_fs("nfs4") and not is_network_fs("exfat")


def test_udev_facts_are_read_and_decoded(tmp_path):
    (tmp_path / "b8:1").write_text(
        "S:disk/by-uuid/3A1F-9C2B\n"
        "E:ID_FS_LABEL=My_Music\n"
        "E:ID_FS_LABEL_ENC=My\\x20Music\n"
        "E:ID_FS_UUID=3A1F-9C2B\n"
        "E:ID_FS_TYPE=vfat\n"
        "E:ID_FS_USAGE=filesystem\n"
        "E:ID_BUS=usb\n"
        "E:ID_VENDOR_ENC=SanDisk\\x20\n"
        "E:ID_MODEL=Ultra_Fit\n"
        "E:ID_DRIVE_THUMB=1\n"
    )

    facts = block_facts("8:1", data_dir=str(tmp_path))

    assert facts == BlockFacts(
        label="My Music",
        uuid="3A1F-9C2B",
        fs_type="vfat",
        fs_usage="filesystem",
        bus="usb",
        vendor="SanDisk",
        model="Ultra Fit",
        thumb_drive=True,
    )


def test_a_device_udev_never_saw_has_no_facts(tmp_path):
    assert block_facts("8:99", data_dir=str(tmp_path)) == BlockFacts()
