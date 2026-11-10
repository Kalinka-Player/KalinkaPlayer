"""Stable Core identity, persisted at <state_dir>/server_id.

Renderers report the owner of their active playback session by server_id, so
this must survive a restart, a port change and a rename — anything less and a
recovered Core cannot recognise its own orphaned session. The package makes
the file as it installs, through the same server_id_file; Core makes it only
where that has not run, as in a dev run, from the RPM or in the demo container.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from kalinka_plugin_sdk import paths

from .server_id_file import ensure_id

logger = logging.getLogger(__name__.split(".")[-1])

_server_id: str | None = None


def get_server_id() -> str:
    global _server_id
    if _server_id is not None:
        return _server_id

    path = Path(paths.state_dir()) / "server_id"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _server_id = ensure_id(path)
    except OSError as exc:
        _server_id = str(uuid.uuid4())
        logger.warning(
            "Could not persist server id at %s (%s); using an ephemeral id",
            path,
            exc,
        )
    return _server_id
