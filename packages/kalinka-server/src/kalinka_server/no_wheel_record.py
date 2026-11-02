"""The optional packages pip found no wheel of, kept across installs.

last_install.json holds only the latest install, so install_pending keeps this
record beside it: once pip finds no wheel of a package for this Python and
architecture, the server stops queueing it, whatever is installed after.
Standard library only, as install_pending runs it as root at boot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FILE_NAME = "no_wheel.json"
SCHEMA = 1


def read(path: Path, python: str, architecture: str) -> dict[str, dict[str, str]]:
    """{key: {"pip_spec", "reason"}} as recorded for this Python and
    architecture; empty when there is no record, it cannot be read, or it was
    kept for another interpreter."""
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(record, dict)
        or record.get("python") != python
        or record.get("architecture") != architecture
        or not isinstance(record.get("packages"), dict)
    ):
        return {}
    return {
        str(key): {"pip_spec": entry["pip_spec"], "reason": entry["reason"]}
        for key, entry in record["packages"].items()
        if isinstance(entry, dict)
        and isinstance(entry.get("pip_spec"), str)
        and isinstance(entry.get("reason"), str)
    }


def write(
    path: Path,
    python: str,
    architecture: str,
    packages: dict[str, dict[str, str]],
) -> None:
    """Replace the record whole, atomically; with nothing in it, remove it."""
    if not packages:
        path.unlink(missing_ok=True)
        return
    record = {
        "schema": SCHEMA,
        "python": python,
        "architecture": architecture,
        "packages": packages,
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(record, indent=2))
    os.replace(tmp_path, path)
