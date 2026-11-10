"""Install, update and remove catalog plugins through a root helper.

Core writes a request naming a catalog release, or an installed package, and
fires a trigger. Where the box has kalinka-plugins.service, that runs
:func:`apply` as root beside a running Core — which unloads the plugin when
the helper is about to replace its files, and loads what the helper
installed — so the server keeps playing from everything else. Where it has
only kalinka-restart.service, or where the helper finds that the release
cannot be taken into a running process, the helper stops Core, installs with
apt and starts Core again, as it always did.

Either way the helper fetches the catalog itself, downloads the release's
package, checks the digest the catalog pins and installs it with apt, or
removes the package. Root never acts on a URL, a digest or a free-form
package name chosen by Core: an install or update is only ever a catalog
release and a removal only ever an installed kalinka-plugin-* package.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Callable, Iterator, Literal, Optional
from urllib.parse import urljoin, urlsplit
import zipfile

import httpx
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from kalinka_plugin_sdk import paths

from ..logging_setup import make_handler
from ..tasks import detach
from ..update_check import (
    deb_is_newer,
    forget_live_plugin_operation,
    installed_package_version,
    live_plugin_operation_pending,
    request_live_plugin_operation,
    request_restart,
    restart_pending,
)
from .catalog import PublicCatalog
from .compatibility import CatalogCompatibility
from .payload import SHA256

if TYPE_CHECKING:
    from ..plugin_lifecycle import PluginLifecycle

logger = logging.getLogger("plugin_operations")

REQUEST_FILE = "plugin-request.json"
RESULT_FILE = "plugin-operation.json"
ACK_FILE = "plugin-unload-ack"
RESTART_DROPIN = "/etc/systemd/system/kalinka-restart.service.d/plugins.conf"
RESTART_PATH_UNIT = "/etc/systemd/system/kalinka-restart.path"
LIVE_PATH_UNIT = "/etc/systemd/system/kalinka-plugins.path"
LIVE_SERVICE_UNIT = "/etc/systemd/system/kalinka-plugins.service"
# While it exists, the server package's dpkg trigger leaves Core running.
LIVE_MARKER = "/run/kalinka/plugin-live-operation"
STALE_REQUEST_SECONDS = 15 * 60
ACK_TIMEOUT_S = 60.0
FOLLOW_DEADLINE_S = 15 * 60
ABANDONED = "The update did not finish. Restart the server to bring it back."
EXPIRED = "The request expired before the server carried it out. Try again."
POLL_INTERVAL_S = 1.0
KEEPALIVE_S = 5.0
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_REDIRECTS = 5
SCRATCH_DIR = "/var/tmp"
CORE_UNIT = "kalinka.service"
APT = ["apt-get", "-o", "DPkg::Lock::Timeout=300"]
EXTENSION_SUFFIXES = (".so", ".pyd")
# The steps an operation moves through, as the app shows them; Core's own
# are `preparing` for an uninstall and `loading`.
STEPS: dict[str, tuple[str, ...]] = {
    "install": (
        "downloading",
        "checking",
        "installing_package",
        "installing_environment",
        "loading",
    ),
    "update": (
        "downloading",
        "checking",
        "preparing",
        "installing_package",
        "installing_environment",
        "loading",
    ),
    "uninstall": ("preparing", "checking", "removing_package"),
}
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

    def fresh(self, now: float) -> bool:
        """Whether it was made less than STALE_REQUEST_SECONDS before ``now``,
        a Unix time, and not after it."""
        try:
            made = datetime.fromisoformat(self.requested_at)
        except ValueError:
            return False
        if made.tzinfo is None:
            return False
        return 0 <= now - made.timestamp() < STALE_REQUEST_SECONDS


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


def live_installation_supported() -> bool:
    """True when kalinka-plugins.service runs the helper beside a running Core."""
    return (
        installation_supported()
        and os.path.isfile(LIVE_PATH_UNIT)
        and os.path.isfile(LIVE_SERVICE_UNIT)
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


def _record_of(request: Optional[OperationRequest], now: float) -> dict:
    """A fresh running record of ``request``; of nothing, when the request
    could not be read."""
    return {
        "schema_version": 2,
        "id": request.requested_at if request else None,
        "action": request.action if request else None,
        "plugin_id": request.plugin_id if request else None,
        "distribution": request.distribution if request else None,
        "version": request.version if request else None,
        "status": "running",
        "step": None,
        "step_index": 0,
        "step_count": len(STEPS[request.action]) if request else 0,
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "error": None,
    }


class _Record:
    """The operation's record as the app follows it, written as it moves.

    World-readable: Core reports it, and overlays its own steps on it.
    """

    def __init__(self, path: Path, data: dict, clock: Callable[[], float] = time.time):
        self._path = path
        self._clock = clock
        self.data = data
        self._write()

    @property
    def directory(self) -> Path:
        return self._path.parent

    def step(self, name: str) -> None:
        steps = STEPS[self.data["action"]]
        self.data.update(step=name, step_index=steps.index(name) + 1)
        self._write()

    def touch(self) -> None:
        """A long step is alive: written now and then, so Core can tell it
        from a helper that died."""
        if self._clock() - self.data["updated_at"] >= KEEPALIVE_S:
            self._write()

    def restarting(self) -> None:
        self.data["status"] = "restarting"
        self._write()

    def finish(self, status: str, error: Optional[str] = None) -> None:
        self.data.update(status=status, error=error, finished_at=self._clock())
        self._write()

    def _write(self) -> None:
        self.data["updated_at"] = self._clock()
        _write_json(self._path, self.data, 0o644)


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
    One request is pending at a time, and none while the server is already
    restarting. Where the box runs the helper beside Core, the request is
    followed here: the plugin is unloaded when the helper is about to replace
    its files, and loaded again from what it installed. Elsewhere accepting a
    request restarts the server.
    """

    def __init__(
        self,
        catalog: PublicCatalog,
        compatibility: CatalogCompatibility,
        renderers: Callable[[], tuple[dict, ...]] = lambda: (),
        *,
        state_dir: Callable[[], str] = paths.state_dir,
        supported: Callable[[], bool] = installation_supported,
        live_supported: Callable[[], bool] = live_installation_supported,
        lifecycle: Callable[[], Optional["PluginLifecycle"]] = lambda: None,
        package_version: Callable[[str], str | None] = installed_package_version,
        newer: Callable[[str, str], bool] = deb_is_newer,
        restart: Callable[[], None] = request_restart,
        restarting: Callable[[], bool] = restart_pending,
        live_trigger: Callable[[], None] = request_live_plugin_operation,
        live_pending: Callable[[], bool] = live_plugin_operation_pending,
        live_forget: Callable[[], None] = forget_live_plugin_operation,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        poll_s: float = POLL_INTERVAL_S,
        deadline_s: float = FOLLOW_DEADLINE_S,
    ):
        self._catalog = catalog
        self._compatibility = compatibility
        self._renderers = renderers
        self._state_dir = state_dir
        self._supported = supported
        self._live_supported = live_supported
        self._lifecycle = lifecycle
        self._package_version = package_version
        self._newer = newer
        self._restart = restart
        self._restarting = restarting
        self._live_trigger = live_trigger
        self._live_pending = live_pending
        self._live_forget = live_forget
        self._clock = clock
        self._poll_s = poll_s
        self._deadline_s = deadline_s
        self._submitting = threading.Lock()
        # A live request unloads and records before its pending check: one at a time.
        self._live_lock = asyncio.Lock()
        # Core's part of the operation in flight, or the last one's end.
        self._core: Optional[dict] = None
        self._following: Optional[asyncio.Task] = None

    def capabilities(self) -> dict:
        """An update is only worth reporting where it can also be applied."""
        supported = self._supported()
        return {
            "installation": supported,
            "update_checks": supported,
            "live_installation": supported and self._live_supported(),
        }

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
        """The record of the most recent operation, if any."""
        record = self.operation()
        return None if record["status"] == "idle" else record

    def operation(self) -> dict:
        """The operation in flight or the last one, as the app follows it:
        the helper's record with Core's own steps laid over it, or
        ``{"status": "idle"}`` when none ever ran."""
        helper = self._helper_record()
        core = self._core
        if core is None:
            return helper or {"status": "idle"}
        if helper is None or helper.get("id") != core["id"]:
            return core
        # Core's steps come after the helper's, and its verdict is final once
        # it has given one — a helper that stood still is still "running".
        if helper.get("status") == "ok" or core["status"] != "running":
            return helper | core
        return helper

    def _helper_record(self) -> dict | None:
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
        lifecycle = self._lifecycle() if self._live_supported() else None
        if lifecycle is None:
            await run_in_threadpool(self._submit, request, live=False)
            status = "restarting"
        else:
            await self._submit_live(request, lifecycle)
            status = "applying"
        return {
            "status": status,
            "operation": action,
            "plugin_id": plugin_id,
            "distribution": distribution,
            "version": candidate.version if candidate else installed,
            "installed_version": installed,
        }

    async def _submit_live(
        self, request: OperationRequest, lifecycle: "PluginLifecycle"
    ) -> None:
        """Hand the request to the helper beside a running Core and follow it.

        A removal has nothing to download, so the plugin is unloaded before
        the request goes; an update keeps playing until the helper has the
        package in hand and asks.
        """
        async with self._live_lock:
            if self._in_flight():
                raise _still_applying()
            core = _record_of(request, self._clock().timestamp())
            self._core = core
            try:
                if request.action == "uninstall":
                    core.update(step="preparing", step_index=1)
                    await lifecycle.unload(request.plugin_id, gone=True)
                await run_in_threadpool(self._submit, request, live=True)
            except Exception:
                # Nothing follows a request that never went: the record would stay "running".
                self._core = None
                if request.action == "uninstall":
                    await self._reload(lifecycle, request.plugin_id, purge=False)
                raise
            self._following = detach(self._follow(request, lifecycle, core))

    def _submit(self, request: OperationRequest, *, live: bool) -> None:
        path = Path(self._state_dir()) / REQUEST_FILE
        # Two requests from the thread pool must not both pass the pending check.
        with self._submitting:
            pending = self._pending(path)
            if pending is not None:
                raise Refusal(
                    409,
                    "operation_in_progress",
                    f"The server is already working to {pending.action} "
                    f"{pending.plugin_id}.",
                )
            # A restart under way may be past its helper; a request now would never run.
            if self._restarting():
                raise Refusal(
                    409,
                    "operation_in_progress",
                    "The server is already restarting. Try again once it is back.",
                )
            if self._in_flight():
                raise _still_applying()
            _write_json(path, request.model_dump(), 0o640)
            try:
                if live:
                    self._live_trigger()
                else:
                    self._restart()
            except OSError as exc:
                path.unlink(missing_ok=True)
                raise Refusal(
                    500,
                    "restart_failed",
                    "The server could not start the plugin operation.",
                ) from exc

    def _in_flight(self) -> bool:
        following = self._following
        return (following is not None and not following.done()) or self._live_pending()

    def _pending(self, path: Path) -> OperationRequest | None:
        # A request the helper never consumed must not refuse every later one.
        try:
            pending = OperationRequest.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            return None
        return pending if pending.fresh(self._clock().timestamp()) else None

    async def _follow(
        self, request: OperationRequest, lifecycle: "PluginLifecycle", core: dict
    ) -> None:
        """Keep up with the helper's record until the operation ends.

        An update's plugin is taken down when the helper says it is about to
        replace its files, and the helper is told; what it installed is loaded
        once it reports success. A helper that falls back to a restart ends
        this too: Core is about to go down with it. One whose record has not
        moved for the deadline is given up on — a step may take long, a
        download over a slow link, but not stand still — and a plugin taken
        down for it is listed in error, since its files may be half replaced.
        """
        moved = time.monotonic()
        seen = None
        unloaded = request.action == "uninstall"
        name = request.plugin_id
        while True:
            await asyncio.sleep(self._poll_s)
            record = self._helper_record()
            if record is None or record.get("id") != request.requested_at:
                if time.monotonic() - moved > self._deadline_s:
                    # Left, the trigger would refuse every later request.
                    self._live_forget()
                    self._core_finish(core, "failed", "The installer did not answer.")
                    return
                continue
            if record.get("updated_at") != seen:
                seen = record.get("updated_at")
                moved = time.monotonic()
            status = record.get("status")
            if status == "running":
                if record.get("step") == "preparing" and not unloaded:
                    await lifecycle.suspend(name)
                    unloaded = True
                    self._acknowledge(request.requested_at)
                if time.monotonic() - moved > self._deadline_s:
                    core.update(step=record.get("step"), step_index=record.get("step_index", 0))
                    self._core_finish(core, "failed", "The installer did not finish.")
                    if unloaded and request.action != "uninstall":
                        await lifecycle.abandon(name, ABANDONED)
                    return
                continue
            if status == "ok":
                if request.action != "uninstall":
                    steps = STEPS[request.action]
                    core.update(step="loading", step_index=len(steps))
                    try:
                        await lifecycle.load(name, purge=True)
                    except Exception as exc:  # noqa: BLE001 — the type only, for the record
                        self._core_finish(
                            core, "failed", f"{name} did not set up ({type(exc).__name__})."
                        )
                        return
                self._core_finish(core, "ok")
                return
            if status == "failed" and unloaded:
                # The files it was running on are still there.
                await self._reload(lifecycle, name, purge=False)
            # Failed, or restarting: the helper's record is the truth now.
            if self._core is core:
                self._core = None
            return

    def _acknowledge(self, operation_id: str) -> None:
        _write_json(Path(self._state_dir()) / ACK_FILE, {"id": operation_id}, 0o644)

    def _core_finish(
        self, core: dict, status: str, error: Optional[str] = None
    ) -> None:
        core.update(status=status, error=error, finished_at=self._clock().timestamp())

    @staticmethod
    async def _reload(lifecycle: "PluginLifecycle", name: str, *, purge: bool) -> None:
        try:
            await lifecycle.load(name, purge=purge)
        except Exception as exc:  # noqa: BLE001 — the type only: it is logged, not raised
            logger.warning("%s could not be loaded back (%s)", name, type(exc).__name__)

    def _evaluate(self, snapshot: dict, plugin_id: str) -> dict:
        return _plugin(
            self._compatibility.annotate(snapshot, self._renderers()), plugin_id
        )


def _plugin(snapshot: dict, plugin_id: str) -> dict | None:
    return next((p for p in snapshot["plugins"] if p["id"] == plugin_id), None)


def _still_applying() -> Refusal:
    return Refusal(
        409,
        "operation_in_progress",
        "The server is still applying the last plugin operation.",
    )


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


@dataclass(frozen=True)
class LiveTools:
    """What the helper needs of the box to apply an operation beside a
    running Core, each replaceable for tests."""

    extract_wheel: Callable[[Path, Path], Path]
    pip_report: Callable[[Path], dict]
    installed_distributions: Callable[[], dict[str, str]]
    has_extension_modules: Callable[[Path], bool]
    installed_has_extension_modules: Callable[[str], bool]
    pip_install: Callable[[Path], None]
    wait_for_unload: Callable[[Path, str], bool]
    marker: Callable[[], Iterator[None]]
    before_stop: Callable[[], None]

    @classmethod
    def real(cls) -> "LiveTools":
        return cls(
            extract_wheel=_extract_wheel,
            pip_report=_pip_report,
            installed_distributions=_installed_distributions,
            has_extension_modules=_wheel_has_extension_modules,
            installed_has_extension_modules=_installed_has_extension_modules,
            pip_install=_pip_install,
            wait_for_unload=_wait_for_unload,
            marker=_live_marker,
            # The record says "restarting" a moment before Core is stopped,
            # so a client polling it learns why its connection goes.
            before_stop=lambda: time.sleep(2),
        )


def apply(
    state_dir: Path,
    *,
    fetch_catalog: Callable[[], dict],
    download: Callable[[str, str, int, Path], None],
    run: Callable[[list[str]], str],
    package_version: Callable[[str], str | None],
    live: bool = False,
    tools: Optional[LiveTools] = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Carry out the pending request as root, recording the outcome for Core.

    The request is consumed before anything runs, so a crash cannot replay it
    at every restart. One that is not :meth:`~OperationRequest.fresh` is
    recorded as failed instead: nobody is waiting for it any more, and the
    restart that runs the helper may be for something else. With ``live``
    Core keeps running: it is asked to unload the plugin before its files
    change and loads the result itself — unless the release cannot be taken
    into a running process, when the helper stops Core, installs, and starts
    it again, as without ``live``.
    """
    request_path = state_dir / REQUEST_FILE
    if not request_path.is_file():
        return 0
    result_path = state_dir / RESULT_FILE
    try:
        request = OperationRequest.model_validate_json(request_path.read_bytes())
    except ValueError:
        _Record(result_path, _record_of(None, clock()), clock).finish(
            "failed", "The request could not be read."
        )
        return 1
    finally:
        request_path.unlink(missing_ok=True)
    if not request.fresh(clock()):
        logger.warning(
            "Not carrying out %s %s requested at %s: the request expired",
            request.action,
            request.distribution,
            request.requested_at,
        )
        _Record(result_path, _record_of(request, clock()), clock).finish(
            "failed", EXPIRED
        )
        return 0
    record = _Record(result_path, _record_of(request, clock()), clock)
    logger.info("%s %s", request.action, request.distribution)
    try:
        if request.action == "uninstall":
            _uninstall(request, record, run, package_version, live=live, tools=tools)
        else:
            _install(
                request,
                record,
                fetch_catalog,
                download,
                run,
                package_version,
                live=live,
                tools=tools,
            )
        record.finish("ok")
    except Failure as exc:
        record.finish("failed", str(exc))
    except subprocess.CalledProcessError as exc:
        record.finish(
            "failed", f"{Path(exc.cmd[0]).name} did not complete; see the helper's log."
        )
    except httpx.HTTPError as exc:
        # The message may quote a signed download URL; the type says enough.
        record.finish(
            "failed", f"The package could not be downloaded ({type(exc).__name__})."
        )
    except OSError as exc:
        record.finish("failed", f"{exc.strerror or exc}.")
    except Exception:
        # Anything else would leave the record at "running" for good.
        logger.exception("Plugin operation crashed")
        record.finish("failed", "The installer failed unexpectedly.")
    if record.data["status"] == "failed":
        logger.error("Plugin operation failed: %s", record.data["error"])
    return 0 if record.data["status"] == "ok" else 1


def _install(
    request, record, fetch_catalog, download, run, package_version, *, live, tools
) -> None:
    record.step("downloading")
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
        download(
            artifact["url"],
            artifact["sha256"],
            artifact["size_bytes"],
            package,
            keepalive=record.touch,
        )
        os.chmod(package, 0o644)
        record.step("checking")
        plan = run(APT + ["-s", "install", "-y", "--no-install-recommends", str(package)])
        _check_install_plan(plan)
        in_place = live
        wheel = None
        if live:
            wheel = tools.extract_wheel(package, Path(scratch))
            blocker = _live_blocker(request, plan, wheel, tools)
            if blocker is not None:
                logger.info("Restarting to %s %s: %s", request.action, request.distribution, blocker)
                in_place = False
            elif request.action == "update":
                record.step("preparing")
                if not tools.wait_for_unload(record.directory, request.requested_at):
                    logger.info("Core did not unload %s; restarting instead", request.distribution)
                    in_place = False
        install = APT + ["install", "-y", "--no-install-recommends", str(package)]
        if in_place:
            record.step("installing_package")
            with tools.marker():
                run(install)
            record.step("installing_environment")
            tools.pip_install(wheel)
        elif live:
            record.restarting()
            tools.before_stop()
            run(["systemctl", "stop", CORE_UNIT])
            try:
                record.step("installing_package")
                run(install)
            finally:
                # Started here, not by a restart service: bootstrap installs the wheel.
                run(["systemctl", "start", CORE_UNIT])
        else:
            run(["systemctl", "stop", CORE_UNIT])
            record.step("installing_package")
            run(install)
    if package_version(request.distribution) != artifact["package"]["version"]:
        raise Failure("The package did not end up installed.")


def _live_blocker(request, plan: str, wheel: Path, tools: LiveTools) -> Optional[str]:
    """Why the release cannot be taken into the running server, or None.

    What is already imported cannot be replaced in place: another package
    the apt plan brings, a distribution pip would change that is installed
    already, or compiled modules in either version of the plugin itself.
    """
    own = canonicalize_name(request.distribution)
    for line in plan.splitlines():
        step, _, rest = line.partition(" ")
        name = rest.split(" ", 1)[0]
        if step in {"Inst", "Conf"} and canonicalize_name(name) != own:
            return f"it brings {name}"
    installed = tools.installed_distributions()
    for item in tools.pip_report(wheel).get("install", []):
        info = item.get("metadata", {}) if isinstance(item, dict) else {}
        name = canonicalize_name(str(info.get("name", "")))
        if not name or name == own:
            continue
        if name in installed:
            return f"it changes {name} {installed[name]} to {info.get('version')}"
    if request.action == "update" and (
        tools.has_extension_modules(wheel) or tools.installed_has_extension_modules(own)
    ):
        return "it carries compiled modules"
    return None


def _check_install_plan(plan: str) -> None:
    for line in plan.splitlines():
        step, _, rest = line.partition(" ")
        name = rest.split(" ", 1)[0]
        if step == "Remv":
            raise Failure(f"Installing it would remove {name}.")
        if step in {"Inst", "Conf"} and name in PROTECTED_PACKAGES:
            raise Failure(f"Installing it would replace {name}.")


def _uninstall(request, record, run, package_version, *, live, tools) -> None:
    record.step("checking")
    plan = run(APT + ["-s", "remove", "-y", request.distribution])
    removed = [
        line.split()[1] for line in plan.splitlines() if line.startswith("Remv ")
    ]
    others = [name for name in removed if name != request.distribution]
    if others:
        raise Failure(f"Removing it would also remove {', '.join(others)}.")
    record.step("removing_package")
    remove = APT + ["remove", "-y", request.distribution]
    if live:
        with tools.marker():
            run(remove)
    else:
        run(["systemctl", "stop", CORE_UNIT])
        run(remove)
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
    keepalive: Callable[[], None] = lambda: None,
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
                        keepalive()
                break
        else:
            raise Failure("The package download redirected too many times.")
    if received != size_bytes or digest.hexdigest() != sha256:
        raise Failure("The package is not the one the catalog describes.")


def _run(command: list[str]) -> str:
    logger.info("Running: %s", " ".join(command))
    # check=True would raise past the print below, dropping apt's stdout detail.
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C",
            "DEBIAN_FRONTEND": "noninteractive",
        },
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    completed.check_returncode()
    return completed.stdout


def _pip_env() -> dict[str, str]:
    """pip as bootstrap.sh runs it: piwheels beside PyPI, wheels only, the
    service's cache rather than root's home."""
    cache = "/var/cache/kalinka"
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "XDG_CACHE_HOME": cache,
        "PIP_CACHE_DIR": f"{cache}/pip",
        "PIP_INDEX_URL": os.environ.get("PIP_INDEX_URL", "https://www.piwheels.org/simple"),
        "PIP_EXTRA_INDEX_URL": os.environ.get("PIP_EXTRA_INDEX_URL", "https://pypi.org/simple"),
    }


def _pip(arguments: list[str]) -> None:
    command = [sys.executable, "-m", "pip", *arguments]
    logger.info("Running: %s", " ".join(command))
    subprocess.run(command, check=True, env=_pip_env())


def _extract_wheel(package: Path, scratch: Path) -> Path:
    payload = scratch / "payload"
    subprocess.run(["dpkg-deb", "-x", str(package), str(payload)], check=True)
    wheels = sorted((payload / "opt/kalinka/wheels").glob("*.whl"))
    if len(wheels) != 1:
        raise Failure("The package does not carry one plugin wheel.")
    return wheels[0]


def _pip_report(wheel: Path) -> dict:
    report = wheel.parent / "pip-report.json"
    try:
        _pip(
            [
                "install",
                "--dry-run",
                "--report",
                str(report),
                "--quiet",
                "--upgrade",
                "--no-input",
                "--only-binary=:all:",
                str(wheel),
            ]
        )
        return json.loads(report.read_text())
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise Failure("The plugin's requirements could not be resolved.") from exc


def _installed_distributions() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            found[canonicalize_name(name)] = dist.version or ""
    return found


def _wheel_has_extension_modules(wheel: Path) -> bool:
    with zipfile.ZipFile(wheel) as archive:
        return any(name.endswith(EXTENSION_SUFFIXES) for name in archive.namelist())


def _installed_has_extension_modules(distribution: str) -> bool:
    try:
        files = metadata.distribution(distribution).files or ()
    except metadata.PackageNotFoundError:
        return False
    return any(str(path).endswith(EXTENSION_SUFFIXES) for path in files)


def _pip_install(wheel: Path) -> None:
    try:
        _pip(["install", "--upgrade", "--no-input", "--only-binary=:all:", str(wheel)])
    except subprocess.CalledProcessError as exc:
        raise Failure(
            "The plugin could not be installed into the server's environment."
        ) from exc


def _wait_for_unload(state_dir: Path, operation_id: str) -> bool:
    """Whether Core unloaded the plugin for this operation within the time
    allowed; its acknowledgement is consumed either way."""
    ack = state_dir / ACK_FILE
    deadline = time.monotonic() + ACK_TIMEOUT_S
    while True:
        try:
            if json.loads(ack.read_text()).get("id") == operation_id:
                ack.unlink(missing_ok=True)
                return True
        except (OSError, ValueError, AttributeError):
            pass
        if time.monotonic() >= deadline:
            ack.unlink(missing_ok=True)
            return False
        time.sleep(0.5)


@contextmanager
def _live_marker() -> Iterator[None]:
    marker = Path(LIVE_MARKER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(mode=0o644)
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, handlers=[make_handler()])
    live = "--live" in (sys.argv[1:] if argv is None else argv)
    return apply(
        Path(paths.state_dir()),
        fetch_catalog=_fetch_catalog,
        download=_download,
        run=_run,
        package_version=installed_package_version,
        live=live,
        tools=LiveTools.real() if live else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
