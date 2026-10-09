"""Shared on-demand pip install utilities for subprocess workers."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import subprocess
import sys
import time

logger = logging.getLogger(__name__.split(".")[-1])

# Global state — each process gets its own copy after fork.
_install_failed: set[str] = set()


def resolve_probe_name(import_name: str, aliases: dict[str, str]) -> str:
    """Map an import name to its probe name via an alias table."""
    return aliases.get(import_name, import_name)


def is_import_available(import_name: str, aliases: dict[str, str]) -> bool:
    """Check whether *import_name* (or its alias) is importable."""
    return importlib.util.find_spec(resolve_probe_name(import_name, aliases)) is not None


def ensure_package(
    import_name: str,
    pip_specs: dict[str, str],
    aliases: dict[str, str] | None = None,
) -> bool:
    """Ensure *import_name* is importable, installing via pip if necessary.

    Returns True if the package is available after the call.
    """
    if aliases is None:
        aliases = {}
    if is_import_available(import_name, aliases):
        return True
    if import_name in _install_failed:
        return False
    pip_spec = pip_specs.get(import_name, import_name)
    logger.info("Installing missing dependency '%s' via pip …", pip_spec)
    t0 = time.monotonic()
    try:
        subprocess.run(
            # Wheels only, as install_pending: a box may have no compiler.
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--only-binary=:all:",
                pip_spec,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(
            "pip install %s failed (%.1fs) — will not retry this session:\n%s",
            pip_spec,
            time.monotonic() - t0,
            e.stderr.strip(),
        )
        _install_failed.add(import_name)
        return False
    importlib.invalidate_caches()
    available = is_import_available(import_name, aliases)
    if not available:
        probe = resolve_probe_name(import_name, aliases)
        logger.error("'%s' still not importable after pip install", probe)
        _install_failed.add(import_name)
    else:
        logger.info(
            "pip install %s completed in %.1fs", pip_spec, time.monotonic() - t0
        )
    return available
