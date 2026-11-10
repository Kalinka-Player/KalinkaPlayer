"""The server id's file: what in it counts as an id, and how a new one is written.

Core's server_identity reads and makes its id here. The Debian package also
ships this file as /usr/lib/kalinka/server-id, which the postinst runs as the
package installs and kalinka-server-id.service on the first boot of a streamer
flashed from an image, so the supervisor reports the id before Core first
starts. Keep it stdlib-only: system Python runs it before bootstrap.sh has
built Core's venv.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import uuid
from pathlib import Path

logger = logging.getLogger(__name__.split(".")[-1])

# Past any spelling uuid.UUID takes: a link kalusr aims at a huge file costs nothing.
_LONGEST_READ = 4096


def read_id(path: Path) -> str | None:
    """The id the file holds, or None where there is no file or no id in it."""
    try:
        # No link, no wait: root's postinst must open no device or FIFO kalusr left.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        try:
            if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return str(uuid.UUID(handle.read(_LONGEST_READ).decode().strip()))
        except (OSError, ValueError):
            pass
    logger.warning("Ignoring malformed server id at %s", path)
    return None


def ensure_id(path: Path) -> str:
    """The id the file holds, or a new one written there to outlast a power cut.

    Raises OSError where the new one cannot be written.
    """
    existing = read_id(path)
    if existing is not None:
        return existing
    made = str(uuid.uuid4())
    new = path.with_name(path.name + ".new")
    # The postinst runs as root in a directory kalusr owns: follow no link left here.
    new.unlink(missing_ok=True)
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as handle:
        _give_to_directory_owner(handle.fileno(), path.parent)
        handle.write(made + "\n")
        handle.flush()
        # A lost id orphans Core's renderer sessions and the app's pins.
        os.fsync(handle.fileno())
    os.replace(new, path)
    try:
        _sync_directory(path.parent)
    except OSError as exc:
        # The id is in place: a caller taking this for none would run on another.
        logger.warning("Could not sync %s after writing the id (%s)", path.parent, exc)
    logger.info("Generated server id %s at %s", made, path)
    return made


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _give_to_directory_owner(fd: int, directory: Path) -> None:
    # Only root can give a file away, and a chown by path could land on a link.
    if os.geteuid() != 0:
        return
    owner, made = os.stat(directory), os.fstat(fd)
    if (owner.st_uid, owner.st_gid) != (made.st_uid, made.st_gid):
        os.fchown(fd, owner.st_uid, owner.st_gid)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ensure_id(Path(sys.argv[1]))
