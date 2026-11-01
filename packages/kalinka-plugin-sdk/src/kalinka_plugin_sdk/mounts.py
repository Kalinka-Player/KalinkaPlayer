"""SDK 3.20: what is mounted on this machine, and what udev knows of a disk.

Reads ``/proc/self/mountinfo`` and udev's database under ``/run/udev/data``,
both readable without privileges, so the server and any plugin can tell a
drive that is plugged in from a folder that is merely empty.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

#: Where Kalinka mounts the drives and shares it manages, one directory each.
KALINKA_MEDIA_ROOT = "/media/kalinka"

UDEV_DATA_DIR = "/run/udev/data"

_NETWORK_FS = {
    "cifs",
    "smb3",
    "smbfs",
    "sshfs",
    "fuse.sshfs",
    "davfs",
    "fuse.davfs2",
    "9p",
    "ceph",
    "fuse.ceph",
    "glusterfs",
    "fuse.glusterfs",
    "afpfs",
}

_SMB_FS = {"cifs", "smb3", "smbfs"}


@dataclass(frozen=True)
class Mount:
    """One mountinfo row: where a filesystem is mounted, its type and source.

    @param device The kernel's ``major:minor``. Rows sharing it are views
        of one filesystem.
    @param fs_root Which part of that filesystem is mounted: ``/`` for the
        whole of it, a directory for a bind mount, a subvolume for btrfs.
    @note Both default to empty, as they are for a Mount built by hand
        rather than read from mountinfo.
    """

    mount_point: str
    fs_type: str
    source: str
    device: str = ""
    fs_root: str = ""


def _unescape(field: str) -> str:
    if "\\" not in field:
        return field
    out = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def list_mounts(text: Optional[str] = None) -> list[Mount]:
    """Parse mountinfo rows, in mount order (later rows stack on earlier)."""
    if text is None:
        try:
            # One mount with a name that is not UTF-8 must not cost the rest.
            with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
        except OSError:
            return []
    mounts: list[Mount] = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" - ")
        if not sep:
            continue
        head_fields = head.split(" ")
        tail_fields = tail.split(" ")
        if len(head_fields) < 5 or len(tail_fields) < 2:
            continue
        mounts.append(
            Mount(
                mount_point=_unescape(head_fields[4]),
                fs_type=tail_fields[0],
                source=_unescape(tail_fields[1]),
                device=head_fields[2],
                fs_root=_unescape(head_fields[3]),
            )
        )
    return mounts


def covers(mount_point: str, path: str) -> bool:
    """Whether ``path`` is at or below ``mount_point``."""
    return (
        mount_point == "/"
        or path == mount_point
        or path.startswith(mount_point + os.sep)
    )


def covering_mount(path: str, mounts: Optional[list[Mount]] = None) -> Optional[Mount]:
    """The mount `path` currently lives on: longest matching mount point,
    later rows winning a tie (they are stacked on top)."""
    if mounts is None:
        mounts = list_mounts()
    best: Optional[Mount] = None
    for m in mounts:
        if covers(m.mount_point, path) and (
            best is None or len(m.mount_point) >= len(best.mount_point)
        ):
            best = m
    return best


def is_network_fs(fs_type: str) -> bool:
    """Whether a server, rather than a disk on this machine, answers for a
    filesystem of this type."""
    return fs_type.startswith("nfs") or fs_type in _NETWORK_FS


def share_protocol(fs_type: str) -> Optional[str]:
    """The protocol a user knows a mounted share by, as "SMB" or "NFS"; None
    for any other filesystem."""
    if fs_type.startswith("nfs"):
        return "NFS"
    return "SMB" if fs_type in _SMB_FS else None


@dataclass(frozen=True)
class BlockFacts:
    """What udev recorded about one block device.

    Every field is empty when udev did not say: a device it has not
    finished with, or one it never saw.
    """

    label: str = ""
    uuid: str = ""
    fs_type: str = ""
    fs_usage: str = ""
    bus: str = ""
    vendor: str = ""
    model: str = ""
    thumb_drive: bool = False


_HEX_ESCAPE = re.compile(r"\\x([0-9A-Fa-f]{2})")


def _decode_udev(value: str) -> str:
    raw = _HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), value)
    try:
        return raw.encode("latin-1").decode("utf-8").strip()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw.strip()


def _udev_value(env: dict[str, str], key: str) -> str:
    encoded = env.get(f"{key}_ENC")
    if encoded:
        return _decode_udev(encoded)
    return env.get(key, "").replace("_", " ").strip()


def block_facts(device: str, data_dir: str = UDEV_DATA_DIR) -> BlockFacts:
    """udev's record of the block device ``major:minor``, as mountinfo names it."""
    env: dict[str, str] = {}
    try:
        with open(os.path.join(data_dir, f"b{device}"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("E:"):
                    key, _, value = line[2:].rstrip("\n").partition("=")
                    env[key] = value
    except OSError:
        return BlockFacts()
    return BlockFacts(
        label=_udev_value(env, "ID_FS_LABEL"),
        uuid=env.get("ID_FS_UUID", ""),
        fs_type=env.get("ID_FS_TYPE", ""),
        fs_usage=env.get("ID_FS_USAGE", ""),
        bus=env.get("ID_BUS", ""),
        vendor=_udev_value(env, "ID_VENDOR"),
        model=_udev_value(env, "ID_MODEL"),
        thumb_drive=env.get("ID_DRIVE_THUMB") == "1",
    )
