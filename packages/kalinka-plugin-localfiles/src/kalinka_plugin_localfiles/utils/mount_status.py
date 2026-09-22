"""Mount awareness for music folders on the local filesystem.

A music folder is often a mounted network share (NFS/CIFS, possibly managed
by autofs), and an unmounted share is indistinguishable from a deleted
library by plain ``os.path`` checks. This module reads
``/proc/self/mountinfo`` to tell the two apart.

It is what :class:`~..storage.local.LocalStorage` answers availability
questions with; the bounding and retrying around :func:`probe_root` belong
to every storage alike and live in :class:`~..storage.base.FileStorage`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..storage.base import RootStatus

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
            with open("/proc/self/mountinfo", encoding="utf-8") as f:
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


def _covers(mount_point: str, path: str) -> bool:
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
        if _covers(m.mount_point, path) and (
            best is None or len(m.mount_point) >= len(best.mount_point)
        ):
            best = m
    return best


def is_network_fs(fs_type: str) -> bool:
    """Whether a server, rather than a disk on this machine, answers for a
    filesystem of this type."""
    return fs_type.startswith("nfs") or fs_type in _NETWORK_FS


def autofs_pending(root: str, mounts: Optional[list[Mount]] = None) -> bool:
    """True while an autofs mount is what answers for ``root`` — the real
    filesystem is not mounted. Reads mountinfo only, so it never triggers
    the automount itself."""
    cov = covering_mount(root, mounts)
    return cov is not None and cov.fs_type == "autofs"


def probe_root(root: str, mounts: Optional[list[Mount]] = None) -> RootStatus:
    """Blocking availability probe. The stat deliberately touches ``root`` so
    a pending automount is triggered. Blocks for as long as a hung mount
    takes, which is why every caller reaches it through
    :meth:`~..storage.base.FileStorage.probe_root`."""
    try:
        accessible = os.path.isdir(root) and os.access(root, os.R_OK | os.X_OK)
    except OSError:
        accessible = False
    if mounts is None:
        mounts = list_mounts()
    cov = covering_mount(root, mounts)
    fs_type = cov.fs_type if cov else None
    identity = f"{cov.fs_type} {cov.source}" if cov else None
    is_autofs = any(m.fs_type == "autofs" and _covers(m.mount_point, root) for m in mounts)
    is_network = fs_type is not None and is_network_fs(fs_type)
    if fs_type == "autofs":
        return RootStatus(
            root=root,
            available=False,
            reason="the automounter has not mounted it",
            fs_type=fs_type,
            is_network=is_network,
            is_autofs=True,
            identity=identity,
        )
    if not accessible:
        return RootStatus(
            root=root,
            available=False,
            reason="the folder is missing or not readable",
            fs_type=fs_type,
            is_network=is_network,
            is_autofs=is_autofs,
            identity=identity,
        )
    try:
        # Listed for the failure, not for the contents: the access bits can
        # say yes where the filesystem underneath still refuses.
        with os.scandir(root) as entries:
            next(iter(entries), None)
    except OSError:
        return RootStatus(
            root=root,
            available=False,
            reason="the folder could not be listed",
            fs_type=fs_type,
            is_network=is_network,
            is_autofs=is_autofs,
            identity=identity,
        )
    return RootStatus(
        root=root,
        available=True,
        reason="",
        fs_type=fs_type,
        is_network=is_network,
        is_autofs=is_autofs,
        identity=identity,
    )
