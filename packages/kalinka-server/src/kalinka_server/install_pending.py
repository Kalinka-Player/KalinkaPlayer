"""Privileged install helper invoked by bootstrap.sh.

Reads a pending-installs request file written by the kalusr-side server,
intersects it with deb-shipped manifests (the static allow-list), and
calls `pip install` for each allowed key. Writes a per-run audit record
to last_install.json.

Runs as root from /opt/kalinka/venv/bin/python (the same venv the server
runs in). Uses stdlib only to keep the bootstrap path minimal.

Usage:
    python -m kalinka_server.install_pending \\
        <pending.json> <manifests_dir> <last_install.json>

Exit codes:
    0 — finished (even if some installs failed; partial is fine)
    1 — bad arguments, unreadable inputs, or write of audit log failed

The helper never blocks the system: if pip itself fails, the failure is
logged to last_install.json and the helper exits 0 so kalinka.service
still starts. The server can read last_install.json to surface results.
pip installs wheels only, so a failed entry whose package has no wheel for
this machine says so in its ``reason``, once every index pip asked has
answered.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from .logging_setup import make_handler


logger = logging.getLogger("install_pending")
logging.basicConfig(level=logging.INFO, handlers=[make_handler()])

# pip's own wording. The package it names may be a dependency of the spec.
_NOT_FOUND = re.compile(r"No matching distribution found for (.+)")
# pip's debug log names each index page it asks for, and logs it fetched
# only once the index answered with a listing.
_PAGE_ASKED = re.compile(r"Getting page (\S+)")
_PAGE_ANSWERED = re.compile(r"Fetched page (\S+) as ")
_PAGE_ABSENT = re.compile(r"Could not fetch URL (\S+): 404 ")


class PipRun(NamedTuple):
    """One pip install: its exit code, its stderr, and, when it failed, its
    debug log, which is empty when pip wrote none."""

    returncode: int
    stderr: str
    log: str


def _load_manifests(manifests_dir: Path) -> dict[str, dict]:
    """Build {key: spec_dict} registry from every <plugin>.json in the dir."""
    registry: dict[str, dict] = {}
    if not manifests_dir.is_dir():
        logger.info("No manifests directory at %s; nothing to install", manifests_dir)
        return registry

    for manifest_path in sorted(manifests_dir.glob("*.json")):
        try:
            data = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping malformed manifest %s: %s", manifest_path, e)
            continue
        packages = data.get("packages") or {}
        if not isinstance(packages, dict):
            logger.warning("Manifest %s has no 'packages' object", manifest_path)
            continue
        for key, spec in packages.items():
            if not isinstance(spec, dict) or "pip_spec" not in spec:
                logger.warning(
                    "Manifest %s key %r missing pip_spec; skipping",
                    manifest_path,
                    key,
                )
                continue
            if key in registry and registry[key]["pip_spec"] != spec["pip_spec"]:
                logger.warning(
                    "Key %r declared by multiple plugins with different pip_spec "
                    "(%r vs %r); using first seen",
                    key,
                    registry[key]["pip_spec"],
                    spec["pip_spec"],
                )
                continue
            registry.setdefault(key, spec)
    return registry


def _pip_install(pip_spec: str) -> PipRun:
    """Invoke pip install for a single package spec.

    Transitive deps are accepted by design — pinning every transitive of
    e.g. essentia-tensorflow is impractical, and the static allow-list of
    top-level specs is the security boundary we care about. Wheels only: a
    source build holds kalinka.service's start for as long as a compile
    takes on a Pi, and needs a toolchain on the box.
    """
    with tempfile.TemporaryDirectory(prefix="install_pending.") as scratch:
        log_path = Path(scratch) / "pip.log"
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-input",
            "--only-binary=:all:",
            "--log",
            str(log_path),
            pip_spec,
        ]
        logger.info("Running: %s", " ".join(cmd))
        completed = subprocess.run(cmd, capture_output=True, text=True)
        log = ""
        if completed.returncode != 0:
            try:
                log = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return PipRun(completed.returncode, completed.stderr, log)


def _every_index_answered(pip_log: str) -> bool:
    """Whether pip heard back from every index page it asked for; a 4xx or
    5xx answer, or none, ends in the same not-found error as a missing
    wheel. A 404 still counts once another index listed the project, as
    when bootstrap.sh's piwheels has no page for a package PyPI carries."""
    asked = set(_PAGE_ASKED.findall(pip_log))
    answered = set(_PAGE_ANSWERED.findall(pip_log))
    listed = {_project_of(page) for page in answered}
    absent = {
        page for page in _PAGE_ABSENT.findall(pip_log) if _project_of(page) in listed
    }
    return bool(pip_log) and asked <= answered | absent


def _project_of(page: str) -> str:
    return page.rstrip("/").rsplit("/", 1)[-1]


def _no_wheel_reason(run: PipRun) -> str | None:
    """Name the package pip found no wheel of for this machine, when that is
    known to be why it failed."""
    not_found = _NOT_FOUND.search(run.stderr or "")
    if not_found is None or not _every_index_answered(run.log):
        return None
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    machine = _wheel_machine(platform.machine(), sys.maxsize > 2**32)
    return (
        f"No prebuilt {not_found.group(1).strip()} for Python {python} "
        f"on {machine}"
    )


def _wheel_machine(kernel_machine: str, python_is_64bit: bool) -> str:
    """The architecture pip wants wheels for. A 32-bit Python on a 64-bit
    kernel, as on 32-bit Raspberry Pi OS on a Pi 4 or 5, runs armv7l or
    i686 code though the kernel reports aarch64 or x86_64."""
    if python_is_64bit:
        return kernel_machine
    return {"aarch64": "armv7l", "x86_64": "i686"}.get(
        kernel_machine, kernel_machine
    )


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write(
            "usage: install_pending <pending.json> <manifests_dir> "
            "<last_install.json>\n"
        )
        return 1

    pending_path = Path(argv[1])
    manifests_dir = Path(argv[2])
    last_install_path = Path(argv[3])

    if not pending_path.is_file():
        logger.info("No pending file at %s; nothing to do", pending_path)
        return 0

    try:
        pending = json.loads(pending_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Cannot read pending file %s: %s", pending_path, e)
        return 1

    requested = pending.get("requested")
    if not isinstance(requested, list):
        logger.error("Pending file missing 'requested' list; got %r", pending)
        # Move it aside so we don't keep hitting the same error.
        try:
            pending_path.rename(
                pending_path.with_suffix(f".malformed.{int(time.time())}")
            )
        except OSError:
            pass
        return 1

    # Move the pending file aside *before* we touch pip. A long download
    # on a slow link can outlast systemd's TimeoutStartSec, an apt deb
    # upgrade, or a hand-pulled power plug — and on any of those the
    # next bootstrap would otherwise re-find the same pending file and
    # restart the install from scratch, infinite loop. The
    # .in_progress.<ts> name preserves an audit trail of what was
    # attempted; the canonical pending_installs.json is gone, so the
    # next boot finds nothing to do. Retry path is via re-toggling the
    # subfeature in the UI — that writes a fresh pending file.
    started_at = time.time()
    in_progress_path = pending_path.with_suffix(
        f".in_progress.{int(started_at)}"
    )
    try:
        pending_path.rename(in_progress_path)
    except OSError as e:
        logger.error(
            "Cannot reserve pending file %s as %s: %s",
            pending_path,
            in_progress_path,
            e,
        )
        return 1
    logger.info("Reserved pending request as %s", in_progress_path)

    registry = _load_manifests(manifests_dir)
    logger.info(
        "Allow-list keys available: %s",
        sorted(registry.keys()) or "(none)",
    )
    results: list[dict] = []
    for raw_key in requested:
        key = str(raw_key)
        if key not in registry:
            logger.warning("Refusing %r: not in allow-list", key)
            results.append({"key": key, "status": "rejected_unknown_key"})
            continue
        spec = registry[key]
        pip_spec = spec["pip_spec"]
        completed = _pip_install(pip_spec)
        entry: dict = {
            "key": key,
            "pip_spec": pip_spec,
            "returncode": completed.returncode,
        }
        if completed.returncode == 0:
            entry["status"] = "ok"
            logger.info("Installed %s (%s)", key, pip_spec)
        else:
            entry["status"] = "failed"
            entry["stderr"] = (completed.stderr or "").strip()[-4000:]
            reason = _no_wheel_reason(completed)
            if reason is not None:
                entry["reason"] = reason
            logger.error(
                "pip install %s failed (rc=%d): %s",
                pip_spec,
                completed.returncode,
                entry["stderr"],
            )
        results.append(entry)

    audit = {
        "schema": 1,
        "started_at": started_at,
        "finished_at": time.time(),
        "requested": list(requested),
        "results": results,
    }

    try:
        last_install_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = last_install_path.with_suffix(last_install_path.suffix + ".tmp")
        tmp.write_text(json.dumps(audit, indent=2))
        os.replace(tmp, last_install_path)
    except OSError as e:
        logger.error("Failed to write audit log %s: %s", last_install_path, e)
        # Still try to move the pending file out of the way below.

    # Rename the in-progress audit trail to .done or .failed depending
    # on the outcome. If this rename fails the file simply stays as
    # .in_progress.<ts> — still a valid record, just less informative.
    any_failed = any(r.get("status") == "failed" for r in results)
    any_rejected = any(
        r.get("status") == "rejected_unknown_key" for r in results
    )
    suffix = ".failed" if (any_failed or any_rejected) else ".done"
    archive = pending_path.with_suffix(f"{suffix}.{int(started_at)}")
    try:
        shutil.move(str(in_progress_path), str(archive))
    except OSError as e:
        logger.error(
            "Failed to rename %s to %s: %s",
            in_progress_path,
            archive,
            e,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
