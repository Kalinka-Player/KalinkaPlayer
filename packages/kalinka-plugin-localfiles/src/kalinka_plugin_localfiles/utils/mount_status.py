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
from typing import Optional

from kalinka_plugin_sdk.mounts import (
    Mount,
    block_facts,
    covering_mount,
    covers,
    is_network_fs,
    list_mounts,
    share_protocol,
)

from ..storage.base import RootStatus

__all__ = [
    "Mount",
    "autofs_pending",
    "covering_mount",
    "is_network_fs",
    "list_mounts",
    "probe_root",
    "share_protocol",
]


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
    legacy = f"{cov.fs_type} {cov.source}" if cov else None
    uuid = block_facts(cov.device).uuid if cov and cov.source.startswith("/dev/") else ""
    identity = f"{cov.fs_type} uuid:{uuid}" if uuid else legacy
    is_autofs = any(m.fs_type == "autofs" and covers(m.mount_point, root) for m in mounts)
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
            legacy_identity=legacy,
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
            legacy_identity=legacy,
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
            legacy_identity=legacy,
        )
    return RootStatus(
        root=root,
        available=True,
        reason="",
        fs_type=fs_type,
        is_network=is_network,
        is_autofs=is_autofs,
        identity=identity,
        legacy_identity=legacy,
    )
