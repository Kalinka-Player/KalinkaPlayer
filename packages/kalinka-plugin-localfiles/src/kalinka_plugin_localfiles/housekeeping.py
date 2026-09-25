"""Folders and files a NAS, a desktop or a filesystem keeps for itself.

Recycle bins, snapshots, thumbnail caches and trash hold copies of music, or
files named like music, that are not the library. Indexed, they list an album
deleted on a Synology or a QNAP again from its bin — with the same identity,
since the NAS moved it there — repeat the whole library once per visible
snapshot, and log an error for every resource fork macOS writes beside a
track it copied onto a share.
"""

from __future__ import annotations

#: Housekeeping folders by name, compared case-insensitively. Hidden folders
#: are skipped as a class and need no entry here.
_HOUSEKEEPING = frozenset(
    name.casefold()
    for name in (
        # Synology
        "#recycle",
        "#snapshot",
        "@eaDir",
        "@tmp",
        # QNAP
        "@Recycle",
        "@Recently-Snapshot",
        "@__thumb",
        # Windows
        "$RECYCLE.BIN",
        "RECYCLER",
        "System Volume Information",
        # ext filesystems
        "lost+found",
    )
)


def is_housekeeping_dir(name: str) -> bool:
    """Whether a folder called ``name`` is one the library never walks.

    Hidden folders count: ``.snapshot``, ``.zfs``, ``.Trash-1000``,
    ``.recycle``, ``.AppleDouble`` and the rest are all somebody's
    housekeeping, and no player looks for music behind a dot.
    """
    return name.startswith(".") or name.casefold() in _HOUSEKEEPING


def is_hidden_file(name: str) -> bool:
    """Whether a file called ``name`` is hidden, which takes in the ``._``
    resource forks macOS writes beside every file it copies to a share."""
    return name.startswith(".")


def in_housekeeping(path: str, root: str, is_dir: bool = False) -> bool:
    """Whether ``path`` lies in a housekeeping folder below ``root``.

    Only the folders below the configured root are judged, so a library
    someone keeps under a hidden folder of their own is still theirs.

    @param path Canonical, and inside ``root``; one that does not start with
        it is not judged.
    @param is_dir Whether ``path`` is itself a folder to judge.
    """
    if not path.startswith(root):
        return False
    parts = path[len(root):].strip("/").split("/")
    folders = parts if is_dir else parts[:-1]
    return any(is_housekeeping_dir(part) for part in folders if part)
