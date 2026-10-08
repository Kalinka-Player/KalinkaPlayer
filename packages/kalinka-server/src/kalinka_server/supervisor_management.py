"""Box management for settings and the privileged restart service.

The Debian package also ships this file as /opt/kalinka/manage-supervisor.py.
Keep it stdlib-only: system Python runs it outside Core's filesystem sandbox,
before kalinka-restart.service restarts Core. It reads only a boolean from the
saved settings; no package name, command or download URL comes from the client.
Ordinary boots and upgrades never reconcile this setting or retry a failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

MANAGE_PATH = "base_config.server.manage_box"
STATUS_PATH = "base_config.server.supervisor_status"
PACKAGE = "kalinka-supervisor"
HELPER = Path("/opt/kalinka/manage-supervisor.py")
INSTALLER = Path("/opt/kalinka/install-supervisor.sh")
RESULT_FILE = "supervisor-management.json"


def _package_state() -> tuple[str, str]:
    """Read the package database, not a saved preference or HTTP availability."""
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", PACKAGE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return "not-installed", ""  # Non-Debian host.
    status, _, version = result.stdout.strip().partition(" ")
    if result.returncode == 0 and status:
        return status, version
    if result.returncode not in (0, 1):
        raise RuntimeError("Could not read the Supervisor package state")
    return "not-installed", ""


def installed_version() -> str | None:
    status, version = _package_state()
    return version if status == "installed" and version else None


def _matches(desired: bool, status: str) -> bool:
    # A half-installed package is neither successfully enabled nor removed.
    return (
        status == "installed"
        if desired
        else status in {"not-installed", "config-files"}
    )


def unavailable_reason() -> str | None:
    if (
        not HELPER.is_file()
        or not INSTALLER.is_file()
        or not Path("/run/systemd/system").is_dir()
    ):
        return (
            "Box management requires a Debian-based Kalinka installation with systemd."
        )
    if not all(
        shutil.which(cmd) for cmd in ("apt-get", "dpkg", "dpkg-query", "systemctl")
    ):
        return (
            "Box management requires a Debian-based Kalinka installation with systemd."
        )
    try:
        arch = subprocess.run(
            ["dpkg", "--print-architecture"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "Could not determine this box's architecture."
    if arch.returncode or arch.stdout.strip() not in {"amd64", "arm64"}:
        return "Box management packages are available for amd64 and arm64 only."
    return None


def _read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected an object in {path.name}")
    return data


def status_text(desired: bool, state_dir: Path) -> str:
    try:
        status, version = _package_state()
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return "Supervisor package state could not be checked."
    if status == "installed":
        text = f"Installed ({version})."
    elif _matches(False, status):
        text = "Not installed."
    else:
        text = f"Installation incomplete ({version})."
    try:
        result = _read_object(state_dir / RESULT_FILE)
    except (OSError, ValueError):
        result = {}
    if not _matches(desired, status):
        operation = "Installation" if desired else "Removal"
        if result.get("requested") is desired and result.get("status") == "failed":
            text += f" {operation} failed: {result.get('error', 'see the server log')}"
            text += " Restart the server to retry."
        elif result.get("requested") is desired and result.get("status") == "running":
            text += f" {operation} was interrupted. Restart the server to retry."
        else:
            text += f" {operation} will run when you apply and restart."
    reason = unavailable_reason()
    if reason:
        text += f" {reason}"
    return text


def _write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".supervisor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream)
            # Core reads the result after the root-owned helper exits.
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run(command: list[str]) -> None:
    # Output goes to the restart unit's journal, not into the settings value.
    subprocess.run(command, check=True)


def apply_saved(config_file: Path, state_dir: Path) -> int:
    result: dict = {"finished_at": None}
    try:
        config = _read_object(config_file)
        if (
            MANAGE_PATH not in config
            or config.get("base_config.server.demo_mode") is True
        ):
            return 0
        desired = config[MANAGE_PATH]
        if type(desired) is not bool:
            raise ValueError("Manage this box must be true or false")
        result.update(requested=desired, started_at=time.time(), status="running")
        if _matches(desired, _package_state()[0]):
            return 0  # In particular, enabling an installed package is not an upgrade.
        reason = unavailable_reason()
        if reason:
            raise RuntimeError(reason)
        _write_result(state_dir / RESULT_FILE, result)
        # Core must go offline before the package operation so Apply waits for
        # the changed installation. The restart unit starts it even on failure.
        _run(["systemctl", "stop", "kalinka.service"])
        apt = ["apt-get", "-o", "DPkg::Lock::Timeout=300"]
        if desired:
            _run(apt + ["update", "--error-on=any"])
            _run(apt + ["install", "-y", "--no-install-recommends", "curl"])
            _run(["bash", str(INSTALLER)])
        else:
            # Keep networking dependencies and persistent setup data. No purge
            # or autoremove: other parts of the box may use those packages.
            _run(apt + ["remove", "-y", PACKAGE])
        if not _matches(desired, _package_state()[0]):
            raise RuntimeError(
                "The Supervisor package state did not change as requested"
            )
        result["status"] = "ok"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"[box management] {exc}", flush=True)
        error = (
            "The package operation did not complete; see the server log."
            if isinstance(exc, subprocess.SubprocessError)
            else str(exc)
        )
        result.update(status="failed", error=error)
    result["finished_at"] = time.time()
    _write_result(state_dir / RESULT_FILE, result)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(
        apply_saved(Path("/etc/kalinka/kalinka_conf.cfg"), Path("/var/lib/kalinka"))
    )
