"""Install, update and remove catalog plugins through the restart service.

Core writes a request naming a catalog release, or an installed package, and
fires the restart trigger. kalinka-restart.service runs :func:`apply` as root
before it starts Core again: it fetches the catalog itself, downloads the
release's package, checks the digest the catalog pins and installs it with
apt, or removes the package. Root never acts on a URL, a digest or a free-form
package name chosen by Core: an install or update is only ever a catalog
release and a removal only ever an installed kalinka-plugin-* package.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Callable, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from kalinka_plugin_sdk import paths

from ..logging_setup import make_handler
from ..update_check import (
    deb_is_newer,
    installed_package_version,
    request_restart,
    restart_pending,
)
from .catalog import PublicCatalog
from .compatibility import CatalogCompatibility
from .payload import SHA256

logger = logging.getLogger("plugin_operations")

REQUEST_FILE = "plugin-request.json"
RESULT_FILE = "plugin-operation.json"
RESTART_DROPIN = "/etc/systemd/system/kalinka-restart.service.d/plugins.conf"
RESTART_PATH_UNIT = "/etc/systemd/system/kalinka-restart.path"
STALE_REQUEST_SECONDS = 15 * 60
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_REDIRECTS = 5
SCRATCH_DIR = "/var/tmp"
CORE_UNIT = "kalinka.service"
APT = ["apt-get", "-o", "DPkg::Lock::Timeout=300"]
# A plugin install may neither replace nor remove what Kalinka itself runs on.
PROTECTED_PACKAGES = frozenset(
    {
        "kalinka-server",
        "kalinka-plugin-sdk",
        "kalinka-renderer",
        "kalinka-supervisor",
        "kalinka-web",
        "kalinka-kiosk",
    }
)

Action = Literal["install", "update", "uninstall"]


class OperationRequest(BaseModel):
    """What Core asks root to do. The helper trusts nothing else about it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    action: Action
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    distribution: str = Field(
        pattern=r"^kalinka-plugin-[a-z0-9][a-z0-9-]*$", max_length=100
    )
    version: str | None = Field(default=None, min_length=1, max_length=100)
    artifact_sha256: SHA256 | None = None
    requested_at: str

    @model_validator(mode="after")
    def check_identity(self) -> OperationRequest:
        if self.distribution == "kalinka-plugin-sdk":
            raise ValueError("The SDK is part of the server")
        if self.action != "uninstall" and (
            self.version is None or self.artifact_sha256 is None
        ):
            raise ValueError("An install names a release and its package digest")
        return self


@dataclass(frozen=True)
class Candidate:
    """The release the preflight would install, with its Debian version."""

    version: str
    artifact_sha256: str
    package_version: str


class Refusal(Exception):
    """Why a request was not accepted, in the shape the route answers with."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Failure(Exception):
    """The helper could not complete the operation; the text is what it records."""


def installation_supported() -> bool:
    """True when kalinka-restart.service runs the helper on this installation."""
    return (
        paths.prefix() == "/"
        and os.path.isfile(RESTART_DROPIN)
        and os.path.isfile(RESTART_PATH_UNIT)
    )


def _write_json(path: Path, data: dict, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _reason_text(reason: dict) -> str:
    extra = ", ".join(
        f"{key} {value}" for key, value in reason.items() if key != "code"
    )
    return reason["code"] + (f" ({extra})" if extra else "")


class PluginOperations:
    """Turns a client's install, update or uninstall into a request for the
    root helper, and lists the updates it would carry out.

    Every check runs against Core's own cached catalog and its metadata
    preflight; the helper repeats what matters for trust from its own fetch.
    One request is pending at a time, none is accepted while the server is
    already restarting, and accepting one restarts the server.
    """

    def __init__(
        self,
        catalog: PublicCatalog,
        compatibility: CatalogCompatibility,
        renderers: Callable[[], tuple[dict, ...]] = lambda: (),
        *,
        state_dir: Callable[[], str] = paths.state_dir,
        supported: Callable[[], bool] = installation_supported,
        package_version: Callable[[str], str | None] = installed_package_version,
        newer: Callable[[str, str], bool] = deb_is_newer,
        restart: Callable[[], None] = request_restart,
        restarting: Callable[[], bool] = restart_pending,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._catalog = catalog
        self._compatibility = compatibility
        self._renderers = renderers
        self._state_dir = state_dir
        self._supported = supported
        self._package_version = package_version
        self._newer = newer
        self._restart = restart
        self._restarting = restarting
        self._clock = clock
        self._submitting = threading.Lock()

    def capabilities(self) -> dict:
        """An update is only worth reporting where it can also be applied."""
        supported = self._supported()
        return {"installation": supported, "update_checks": supported}

    async def updates(self) -> dict:
        """Newer compatible releases of the plugin packages dpkg has installed."""
        unavailable = {
            "status": "unavailable",
            "updates": [],
            "automatic_updates_enabled": False,
        }
        if not self._supported():
            return unavailable | {"reason": "installer_unavailable"}
        snapshot = self._catalog.snapshot()
        if snapshot["status"] not in {"available", "stale"}:
            return unavailable | {"reason": "catalog_unavailable"}
        annotated = await run_in_threadpool(
            self._compatibility.annotate, snapshot, self._renderers()
        )
        updates = []
        for plugin in annotated["plugins"]:
            candidate = _candidate(plugin)
            if candidate is None:
                continue
            installed = await run_in_threadpool(
                self._package_version, plugin["distribution"]
            )
            if installed is not None and self._newer(
                candidate.package_version, installed
            ):
                updates.append(
                    {
                        "plugin_id": plugin["id"],
                        "distribution": plugin["distribution"],
                        "version": candidate.version,
                        "installed_version": installed,
                    }
                )
        return {
            "status": "available",
            "catalog_revision": snapshot["revision"],
            "last_successful_check": snapshot["last_successful_check"],
            "updates": updates,
            "automatic_updates_enabled": False,
        }

    def last_result(self) -> dict | None:
        """What the helper recorded about the most recent operation, if any."""
        try:
            data = json.loads((Path(self._state_dir()) / RESULT_FILE).read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def request(self, action: Action, plugin_id: str) -> dict:
        if not self._supported():
            raise Refusal(
                503,
                "installer_unavailable",
                "This server cannot install or remove plugins: it is not "
                "managed by the Debian package's restart service.",
            )
        snapshot = self._catalog.snapshot()
        if snapshot["status"] not in {"available", "stale"}:
            raise Refusal(
                503,
                "catalog_unavailable",
                "The plugin catalog is not available yet. Try again shortly.",
            )
        plugin = _plugin(snapshot, plugin_id)
        if plugin is None:
            raise Refusal(404, "unknown_plugin", "The catalog lists no such plugin.")
        if plugin["delivery"] != "independent":
            raise Refusal(
                409,
                "bundle_managed",
                f"{plugin['name']} is part of the server bundle: it is installed "
                "and updated together with the server.",
            )
        distribution = plugin["distribution"]
        installed = await run_in_threadpool(self._package_version, distribution)
        if action == "install" and installed is not None:
            raise Refusal(
                409,
                "already_installed",
                f"{plugin['name']} {installed} is already installed.",
            )
        if action != "install" and installed is None:
            raise Refusal(
                409,
                "not_installed",
                f"{plugin['name']} is not installed as a package on this server.",
            )
        candidate = None
        if action != "uninstall":
            plugin = await run_in_threadpool(self._evaluate, snapshot, plugin_id)
            candidate = _candidate(plugin)
            if candidate is None:
                raise Refusal(409, "incompatible", _incompatible(plugin))
            if action == "update" and not self._newer(
                candidate.package_version, installed
            ):
                raise Refusal(
                    409,
                    "up_to_date",
                    f"{plugin['name']} {installed} is the newest release that "
                    "meets this server's requirements.",
                )
        try:
            request = OperationRequest(
                action=action,
                plugin_id=plugin_id,
                distribution=distribution,
                version=candidate.version if candidate else None,
                artifact_sha256=candidate.artifact_sha256 if candidate else None,
                requested_at=self._clock().isoformat(),
            )
        except ValueError:
            raise Refusal(
                409,
                "unsupported_package",
                f"{plugin['name']} is not packaged as a Kalinka plugin.",
            ) from None
        await run_in_threadpool(self._submit, request)
        return {
            "status": "restarting",
            "operation": action,
            "plugin_id": plugin_id,
            "distribution": distribution,
            "version": candidate.version if candidate else installed,
            "installed_version": installed,
        }

    def _evaluate(self, snapshot: dict, plugin_id: str) -> dict:
        return _plugin(
            self._compatibility.annotate(snapshot, self._renderers()), plugin_id
        )

    def _submit(self, request: OperationRequest) -> None:
        path = Path(self._state_dir()) / REQUEST_FILE
        # Two requests from the thread pool must not both pass the pending check.
        with self._submitting:
            pending = self._pending(path)
            if pending is not None:
                raise Refusal(
                    409,
                    "operation_in_progress",
                    f"The server is already restarting to {pending.action} "
                    f"{pending.plugin_id}.",
                )
            # A restart under way may be past its helper; a request now would never run.
            if self._restarting():
                raise Refusal(
                    409,
                    "operation_in_progress",
                    "The server is already restarting. Try again once it is back.",
                )
            _write_json(path, request.model_dump(), 0o640)
            try:
                self._restart()
            except OSError as exc:
                path.unlink(missing_ok=True)
                raise Refusal(
                    500, "restart_failed", "The server could not request a restart."
                ) from exc

    def _pending(self, path: Path) -> OperationRequest | None:
        # A request the helper never consumed must not refuse every later one.
        try:
            pending = OperationRequest.model_validate_json(path.read_bytes())
            requested = datetime.fromisoformat(pending.requested_at)
            age = (self._clock() - requested).total_seconds()
        except (OSError, ValueError, TypeError):
            return None
        return pending if 0 <= age < STALE_REQUEST_SECONDS else None


def _plugin(snapshot: dict, plugin_id: str) -> dict | None:
    return next((p for p in snapshot["plugins"] if p["id"] == plugin_id), None)


def _candidate(plugin: dict) -> Candidate | None:
    """What the preflight annotated as installable on this host, if anything."""
    compatibility = plugin["compatibility"]
    version = compatibility["latest_compatible_version"]
    if version is None:
        return None
    release = next(r for r in compatibility["releases"] if r["version"] == version)
    sha256 = next(
        a["sha256"]
        for a in release["artifacts"]
        if a["status"] == "metadata_compatible"
    )
    package = next(
        a["package"]
        for r in plugin["releases"]
        if r["version"] == version
        for a in r["artifacts"]
        if a["sha256"] == sha256
    )
    return Candidate(version, sha256, package["version"])


def _incompatible(plugin: dict) -> str:
    newest = plugin["compatibility"]["releases"][:1]
    reasons = "; ".join(
        _reason_text(reason) for release in newest for reason in release["reasons"]
    )
    return f"No release of {plugin['name']} meets this server's requirements" + (
        f": {reasons}." if reasons else "."
    )


def apply(
    state_dir: Path,
    *,
    fetch_catalog: Callable[[], dict],
    download: Callable[[str, str, int, Path], None],
    run: Callable[[list[str]], str],
    package_version: Callable[[str], str | None],
) -> int:
    """Carry out the pending request as root, recording the outcome for Core.

    The request is consumed before anything runs, so a crash cannot replay it
    at every restart. The result file is world-readable: Core reports it.
    """
    request_path = state_dir / REQUEST_FILE
    if not request_path.is_file():
        return 0
    result: dict = {
        "schema_version": 1,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "error": None,
    }
    result_path = state_dir / RESULT_FILE
    try:
        try:
            request = OperationRequest.model_validate_json(request_path.read_bytes())
        except ValueError as exc:
            raise Failure("The request could not be read.") from exc
        finally:
            request_path.unlink()
        result.update(
            action=request.action,
            plugin_id=request.plugin_id,
            distribution=request.distribution,
            version=request.version,
        )
        _write_json(result_path, result, 0o644)
        logger.info("%s %s", request.action, request.distribution)
        if request.action == "uninstall":
            _uninstall(request, run, package_version)
        else:
            _install(request, fetch_catalog, download, run, package_version)
        result["status"] = "ok"
    except Failure as exc:
        result.update(status="failed", error=str(exc))
    except subprocess.CalledProcessError as exc:
        result.update(
            status="failed",
            error=f"{exc.cmd[0]} did not complete; see the restart service's log.",
        )
    except httpx.HTTPError as exc:
        # The message may quote a signed download URL; the type says enough.
        result.update(
            status="failed",
            error=f"The package could not be downloaded ({type(exc).__name__}).",
        )
    except OSError as exc:
        result.update(status="failed", error=f"{exc.strerror or exc}.")
    except Exception:
        # Anything else would leave the record at "running" for good.
        logger.exception("Plugin operation crashed")
        result.update(status="failed", error="The installer failed unexpectedly.")
    if result["status"] == "failed":
        logger.error("Plugin operation failed: %s", result["error"])
    result["finished_at"] = time.time()
    _write_json(result_path, result, 0o644)
    return 0 if result["status"] == "ok" else 1


def _install(request, fetch_catalog, download, run, package_version) -> None:
    catalog = fetch_catalog()
    plugin = _plugin(catalog, request.plugin_id)
    gone = Failure("The catalog no longer lists this plugin release. Try again.")
    if (
        plugin is None
        or plugin["distribution"] != request.distribution
        or plugin["delivery"] != "independent"
    ):
        raise gone
    release = next(
        (
            r
            for r in plugin["releases"]
            if r["version"] == request.version and not r["withdrawn"]
        ),
        None,
    )
    artifacts = [
        a
        for a in (release["artifacts"] if release else [])
        if a["format"] == "deb"
        and a["sha256"] == request.artifact_sha256
        and a["package"]["name"] == request.distribution
    ]
    if len(artifacts) != 1:
        raise gone
    artifact = artifacts[0]
    # Disk, not the Pi's tmpfs /tmp; apt fetches local files as _apt, which must read them.
    with tempfile.TemporaryDirectory(
        prefix="kalinka-plugin.", dir=SCRATCH_DIR
    ) as scratch:
        os.chmod(scratch, 0o755)
        package = Path(scratch) / artifact["filename"]
        download(artifact["url"], artifact["sha256"], artifact["size_bytes"], package)
        os.chmod(package, 0o644)
        _check_install_plan(
            run(APT + ["-s", "install", "-y", "--no-install-recommends", str(package)])
        )
        run(["systemctl", "stop", CORE_UNIT])
        run(APT + ["install", "-y", "--no-install-recommends", str(package)])
    if package_version(request.distribution) != artifact["package"]["version"]:
        raise Failure("The package did not end up installed.")


def _check_install_plan(plan: str) -> None:
    for line in plan.splitlines():
        step, _, rest = line.partition(" ")
        name = rest.split(" ", 1)[0]
        if step == "Remv":
            raise Failure(f"Installing it would remove {name}.")
        if step in {"Inst", "Conf"} and name in PROTECTED_PACKAGES:
            raise Failure(f"Installing it would replace {name}.")


def _uninstall(request, run, package_version) -> None:
    plan = run(APT + ["-s", "remove", "-y", request.distribution])
    removed = [
        line.split()[1] for line in plan.splitlines() if line.startswith("Remv ")
    ]
    others = [name for name in removed if name != request.distribution]
    if others:
        raise Failure(f"Removing it would also remove {', '.join(others)}.")
    run(["systemctl", "stop", CORE_UNIT])
    run(APT + ["remove", "-y", request.distribution])
    if package_version(request.distribution) is not None:
        raise Failure("The package is still installed.")


def _fetch_catalog() -> dict:
    catalog = PublicCatalog.from_environment()
    asyncio.run(catalog.refresh())
    snapshot = catalog.snapshot()
    if snapshot["status"] != "available":
        detail = f" ({snapshot['error']})" if snapshot["error"] else ""
        raise Failure(f"The plugin catalog could not be fetched{detail}.")
    return snapshot


def _download_url(value: str) -> None:
    """A redirect may add a signed query, but never leave HTTPS on port 443."""
    parts = urlsplit(value)
    if (
        len(value) > 2048
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise Failure("The package download left HTTPS.")


def _download(
    url: str,
    sha256: str,
    size_bytes: int,
    dest: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    if not 0 < size_bytes <= MAX_ARTIFACT_BYTES:
        raise Failure("The package is larger than this installer accepts.")
    digest = hashlib.sha256()
    received = 0
    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=30,
        headers={"User-Agent": "Kalinka-Plugin-Installer/1"},
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _download_url(url)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    raise Failure(
                        f"The package download answered HTTP {response.status_code}."
                    )
                with open(dest, "wb") as out:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > size_bytes:
                            break
                        digest.update(chunk)
                        out.write(chunk)
                break
        else:
            raise Failure("The package download redirected too many times.")
    if received != size_bytes or digest.hexdigest() != sha256:
        raise Failure("The package is not the one the catalog describes.")


def _run(command: list[str]) -> str:
    logger.info("Running: %s", " ".join(command))
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C",
            "DEBIAN_FRONTEND": "noninteractive",
        },
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    return completed.stdout


def main() -> int:
    logging.basicConfig(level=logging.INFO, handlers=[make_handler()])
    return apply(
        Path(paths.state_dir()),
        fetch_catalog=_fetch_catalog,
        download=_download,
        run=_run,
        package_version=installed_package_version,
    )


if __name__ == "__main__":
    raise SystemExit(main())
