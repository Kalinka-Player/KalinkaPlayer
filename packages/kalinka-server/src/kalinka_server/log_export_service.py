"""The server's one log export: prepared in the background, downloaded once
ready, gone when it expires.

Nothing about an export is persisted. The single server process makes the
in-memory state enough; several worker processes would need a shared lock
first.
"""

import asyncio
import logging
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, List, Literal, Optional

from pydantic import BaseModel

from .config_secrets import credential_detector
from .log_archive import (
    ArchivePart,
    ArchiveResult,
    CancelToken,
    ExportCancelled,
    ExportFailure,
    ExportWarning,
    write_archive,
)
from .log_sources import LOCAL_RENDERER, SERVER, LogSourceCatalog

logger = logging.getLogger(__name__.split(".")[-1])

TEXT_CAP_BYTES = 8 * 1024 * 1024
SPLIT_CAP_BYTES = {SERVER: 6 * 1024 * 1024, LOCAL_RENDERER: 2 * 1024 * 1024}
DEADLINE_SECONDS = 60.0
LIFETIME_SECONDS = 30 * 60.0

_ERROR_MESSAGES = {
    "logs_unreadable": "The server could not read its logs.",
    "collection_timeout": "Collecting the logs took too long.",
    "insufficient_storage": "The server has no room left to write the archive.",
    "export_failed": "The logs could not be exported.",
}

# Why a worker was stopped; only the deadline makes the export fail.
_TIMED_OUT = "deadline"
_WITHDRAWN = "withdrawn"

ExportState = Literal["none", "preparing", "ready", "failed"]


class TimeRange(BaseModel):
    """The range an export covers, fixed when the export is accepted."""

    since: datetime
    until: datetime


class ExportDownload(BaseModel):
    """How a ready archive is offered: its suggested name, size and expiry."""

    filename: str
    size_bytes: int
    expires_at: datetime


class ExportError(BaseModel):
    """Why an export failed: a stable ``code`` and a message fit to show."""

    code: str
    message: str


class ExportStatus(BaseModel):
    """What ``GET /server/logs/export`` answers.

    ``download`` is set only when ready and ``error`` only when failed;
    ``requested_range`` is left out altogether when there is no export.
    """

    state: ExportState
    available_sources: List[str]
    requested_range: Optional[TimeRange] = None
    download: Optional[ExportDownload] = None
    warnings: List[ExportWarning] = []
    error: Optional[ExportError] = None

    def to_json(self) -> dict:
        body = self.model_dump(mode="json")
        if self.requested_range is None:
            del body["requested_range"]
        return body


class ExportInProgress(Exception):
    """A start refused while an export is prepared; ``status`` is that one."""

    def __init__(self, status: ExportStatus) -> None:
        super().__init__("an export is already being prepared")
        self.status = status


class ExportNotReady(Exception):
    """A download refused with no archive ready; ``status`` is the export."""

    def __init__(self, status: ExportStatus) -> None:
        super().__init__("no archive is ready")
        self.status = status


class LogSourceUnavailable(Exception):
    """A source the request needs is not installed.

    ``required`` is True when it is the server's own source — the install is
    missing a part — and False when the client asked for an optional source
    this server does not offer.
    """

    def __init__(self, source: str, required: bool) -> None:
        super().__init__(f"log source {source} is not available")
        self.source = source
        self.required = required


@dataclass
class _Export:
    id: str
    since: datetime
    until: datetime
    token: CancelToken = field(default_factory=CancelToken)
    state: ExportState = "preparing"
    warnings: List[ExportWarning] = field(default_factory=list)
    error: Optional[str] = None
    size_bytes: int = 0
    expires_at: Optional[datetime] = None
    expiry: Optional[asyncio.TimerHandle] = None

    @property
    def filename(self) -> str:
        return f"kalinka-logs-{self.until.strftime('%Y%m%dT%H%M%SZ')}.zip"


@dataclass(frozen=True)
class ArchiveCheckout:
    """A ready archive held for one download.

    ``path`` stays readable until :meth:`release`, whatever happens to the
    export meanwhile.
    """

    path: str
    filename: str

    def release(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class ExportManager:
    """Owns the server's single log export and every change to its state.

    Transitions happen under one lock, on the event loop; the collection and
    compression run on one worker thread at a time. A DELETE that arrives
    before the worker's result is recorded wins: a withdrawn export never
    becomes ready.
    """

    def __init__(
        self,
        directory: str,
        catalog: LogSourceCatalog,
        secrets: Callable[[], Iterable[str]],
        *,
        deadline_seconds: float = DEADLINE_SECONDS,
        lifetime_seconds: float = LIFETIME_SECONDS,
    ) -> None:
        self._directory = directory
        self._catalog = catalog
        self._secrets = secrets
        self._deadline = deadline_seconds
        self._lifetime = lifetime_seconds
        self._lock = threading.Lock()
        self._export: Optional[_Export] = None
        self._last_worker: Optional["asyncio.Task[None]"] = None
        self._downloads = 0

    def open(self) -> None:
        """Start empty: an export does not survive a restart.

        @note Never raises: without its directory every export fails, but the
            server still starts.
        """
        shutil.rmtree(self._directory, ignore_errors=True)
        try:
            os.makedirs(self._directory, mode=0o700, exist_ok=True)
            os.chmod(self._directory, 0o700)
        except OSError as e:
            logger.warning("Log exports will fail: %s", e)

    async def close(self) -> None:
        await self.withdraw()

    def status(self) -> ExportStatus:
        with self._lock:
            return self._status_locked()

    async def start(self, lookback_seconds: int, include_local_renderer: bool) -> ExportStatus:
        """Begin preparing an export of the last ``lookback_seconds``,
        replacing a finished one.

        @raise ExportInProgress While one is being prepared.
        @raise LogSourceUnavailable When a source it needs is not installed.
        """
        available = self._catalog.available()
        with self._lock:
            if self._export is not None and self._export.state == "preparing":
                raise ExportInProgress(self._status_locked())
            if SERVER not in available:
                raise LogSourceUnavailable(SERVER, required=True)
            if include_local_renderer and LOCAL_RENDERER not in available:
                raise LogSourceUnavailable(LOCAL_RENDERER, required=False)
            self._discard_locked()
            until = datetime.now(timezone.utc).replace(microsecond=0)
            names = [SERVER, LOCAL_RENDERER] if include_local_renderer else [SERVER]
            export = _Export(
                id=uuid.uuid4().hex,
                since=until - timedelta(seconds=lookback_seconds),
                until=until,
            )
            caps = SPLIT_CAP_BYTES if len(names) > 1 else {SERVER: TEXT_CAP_BYTES}
            parts = [
                ArchivePart(available[name], caps[name], required=name == SERVER)
                for name in names
            ]
            self._export = export
            self._last_worker = asyncio.create_task(
                self._run(export, parts, credential_detector(self._secrets()), self._last_worker)
            )
            return self._status_locked()

    async def withdraw(self) -> None:
        """Cancel preparation or delete the archive, and return to no
        export. Returns once every cancelled worker has stopped and cleaned
        up, including one an earlier withdrawal is still waiting for."""
        with self._lock:
            self._discard_locked()
            worker = self._last_worker
        if worker is not None:
            await asyncio.wait([worker])

    def checkout(self) -> ArchiveCheckout:
        """Hold the ready archive for one download.

        @raise ExportNotReady When there is no ready archive.
        """
        with self._lock:
            export = self._export
            if export is None or export.state != "ready":
                raise ExportNotReady(self._status_locked())
            self._downloads += 1
            link = os.path.join(self._directory, f"{export.id}.download{self._downloads}")
            os.link(self._archive_path(export), link)
            return ArchiveCheckout(link, export.filename)

    def _archive_path(self, export: _Export) -> str:
        return os.path.join(self._directory, f"{export.id}.zip")

    def _status_locked(self) -> ExportStatus:
        available = sorted(self._catalog.available(), key=[SERVER, LOCAL_RENDERER].index)
        export = self._export
        if export is None:
            return ExportStatus(state="none", available_sources=available)
        download = None
        if export.state == "ready" and export.expires_at is not None:
            download = ExportDownload(
                filename=export.filename,
                size_bytes=export.size_bytes,
                expires_at=export.expires_at,
            )
        error = None
        if export.error is not None:
            error = ExportError(code=export.error, message=_ERROR_MESSAGES[export.error])
        return ExportStatus(
            state=export.state,
            available_sources=available,
            requested_range=TimeRange(since=export.since, until=export.until),
            download=download,
            warnings=export.warnings,
            error=error,
        )

    def _discard_locked(self) -> Optional[_Export]:
        export, self._export = self._export, None
        if export is None:
            return None
        if export.expiry is not None:
            export.expiry.cancel()
        if export.state == "preparing":
            export.token.cancel(_WITHDRAWN)
        elif export.state == "ready":
            self._remove(self._archive_path(export))
        return export

    def _remove(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    async def _run(
        self,
        export: _Export,
        parts: List[ArchivePart],
        carries_credential: Callable[[str], bool],
        previous: Optional["asyncio.Task[None]"],
    ) -> None:
        if previous is not None:
            await asyncio.wait([previous])
        loop = asyncio.get_running_loop()
        deadline = loop.call_later(self._deadline, export.token.cancel, _TIMED_OUT)
        result: Optional[ArchiveResult] = None
        error: Optional[str] = None
        try:
            result = await asyncio.to_thread(
                write_archive,
                self._archive_path(export),
                parts,
                export.since.timestamp(),
                export.until.timestamp(),
                carries_credential,
                export.token,
            )
        except ExportCancelled as e:
            error = "collection_timeout" if e.reason == _TIMED_OUT else None
        except ExportFailure as e:
            logger.warning("Log export failed: %s (%s)", e.code, e.__cause__ or "")
            error = e.code
        except Exception:
            logger.exception("Log export failed")
            error = "export_failed"
        finally:
            deadline.cancel()
        with self._lock:
            if self._export is not export:
                if result is not None:
                    self._remove(self._archive_path(export))
                return
            if result is None:
                export.state = "failed"
                export.error = error or "export_failed"
                return
            export.state = "ready"
            export.size_bytes = result.size_bytes
            export.warnings = result.warnings
            export.expires_at = datetime.now(timezone.utc).replace(
                microsecond=0
            ) + timedelta(seconds=self._lifetime)
            export.expiry = loop.call_later(self._lifetime, self._expire, export)

    def _expire(self, export: _Export) -> None:
        with self._lock:
            if self._export is export:
                self._discard_locked()

