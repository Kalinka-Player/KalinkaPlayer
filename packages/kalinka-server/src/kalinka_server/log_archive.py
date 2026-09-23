"""Turning log sources into the ZIP an export offers for download.

Everything here runs on the export's worker thread. Each source is read
newest first up to its share of the text cap, and kept in memory; a record
that looks like it carries a credential is left out whole before anything
reaches the disk. The archive is then written oldest first beside its final
name and renamed into place.
"""

import errno
import json
import os
import platform
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import entry_points
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from .log_sources import LOCAL_RENDERER, SERVER, LogSource, LogSourceUnreadable
from .version import get_version

MANIFEST_SCHEMA_VERSION = 1

ENTRY_NAMES = {SERVER: "server.log", LOCAL_RENDERER: "local-renderer.log"}
_LABELS = {SERVER: "server", LOCAL_RENDERER: "renderer"}

_ZIP_CHUNK_BYTES = 256 * 1024


class ExportCancelled(Exception):
    """The worker stopped because its token was cancelled."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExportFailure(Exception):
    """The export cannot be completed; ``code`` is the one the API reports."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CancelToken:
    """Stops a worker from another thread.

    The worker polls :meth:`check` between records and between ZIP chunks; a
    read it is blocked in is unblocked by the close function it registered
    with :meth:`closing`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: Optional[str] = None
        self._closers: List[Callable[[], None]] = []

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    def cancel(self, reason: str) -> None:
        """Cancel for ``reason``; only the first reason given sticks."""
        with self._lock:
            if self._reason is not None:
                return
            self._reason = reason
            closers = list(self._closers)
        for close in closers:
            close()

    def check(self) -> None:
        """@raise ExportCancelled Once cancelled."""
        if self._reason is not None:
            raise ExportCancelled(self._reason)

    @contextmanager
    def closing(self, close: Callable[[], None]) -> Iterator[None]:
        """Call ``close`` on cancellation while the block runs."""
        with self._lock:
            already = self._reason is not None
            if not already:
                self._closers.append(close)
        if already:
            close()
        try:
            yield
        finally:
            with self._lock:
                if close in self._closers:
                    self._closers.remove(close)


@dataclass(frozen=True)
class ArchivePart:
    """A source to include, with its share of the text cap.

    A ``required`` source that cannot be read fails the export; any other
    leaves a warning.
    """

    source: LogSource
    cap_bytes: int
    required: bool


@dataclass(frozen=True)
class ExportWarning:
    """A gap in one source's coverage that a reader of the archive should know."""

    source: str
    code: str
    message: str


@dataclass(frozen=True)
class ArchiveResult:
    """A published archive: its size on disk and the gaps in its coverage."""

    size_bytes: int
    warnings: List[ExportWarning]


@dataclass
class _Collected:
    lines: List[bytes] = field(default_factory=list)
    first: Optional[float] = None
    last: Optional[float] = None
    size_bytes: int = 0
    truncated: bool = False
    time_filtered: bool = True
    history_start: Optional[float] = None
    readable: bool = True
    withheld: int = 0


def _iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(int(timestamp), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(
    part: ArchivePart,
    since: float,
    until: float,
    carries_credential: Callable[[str], bool],
    token: CancelToken,
) -> _Collected:
    """The newest records of one source that fit its cap, oldest first,
    without any that ``carries_credential`` flags.

    @raise LogSourceUnreadable If the source cannot be read.
    @raise ExportCancelled If the token was cancelled meanwhile.
    """
    token.check()
    reading = part.source.open(since, until)
    kept: List[tuple] = []
    collected = _Collected()
    with token.closing(reading.close):
        records = reading.records()
        try:
            for record in records:
                token.check()
                if carries_credential(record.text):
                    collected.withheld += 1
                    continue
                line = (record.text + "\n").encode("utf-8")
                if collected.size_bytes + len(line) > part.cap_bytes:
                    collected.truncated = True
                    break
                kept.append((record.timestamp, line))
                collected.size_bytes += len(line)
        except LogSourceUnreadable:
            token.check()
            raise
        finally:
            records.close()
    token.check()
    kept.reverse()
    collected.lines = [line for _, line in kept]
    stamps = [stamp for stamp, _ in kept if stamp is not None]
    collected.first = min(stamps, default=None)
    collected.last = max(stamps, default=None)
    collected.time_filtered = reading.time_filtered
    collected.history_start = reading.history_start
    return collected


def warnings_for(name: str, collected: _Collected, since: float) -> List[ExportWarning]:
    """What a reader of the archive should know about one source's coverage."""
    label = _LABELS.get(name, name)
    if not collected.readable:
        return [
            ExportWarning(name, "source_unreadable", f"The {label}'s logs could not be read.")
        ]
    found: List[ExportWarning] = []
    if collected.truncated:
        found.append(
            ExportWarning(
                name,
                "older_records_dropped",
                f"Older {label} records were left out to keep the archive small.",
            )
        )
    elif not collected.lines:
        found.append(
            ExportWarning(name, "no_records", f"The {label} logged nothing in this period.")
        )
    if (
        not collected.truncated
        and collected.history_start is not None
        and collected.history_start > since
    ):
        found.append(
            ExportWarning(
                name,
                "history_shorter",
                f"The {label}'s logs only go back to {_iso(collected.history_start)}.",
            )
        )
    if collected.withheld:
        noun = "record" if collected.withheld == 1 else "records"
        found.append(
            ExportWarning(
                name,
                "records_withheld",
                f"Left out {collected.withheld} {label} {noun} that seemed to "
                "carry a credential.",
            )
        )
    if not collected.time_filtered:
        found.append(
            ExportWarning(
                name,
                "time_filter_not_applied",
                f"The {label}'s records carry no time, so the newest were taken "
                "whatever their age.",
            )
        )
    return found


def describe_install() -> Dict[str, Any]:
    """Versions and platform, for the manifest."""
    plugins = {}
    for ep in entry_points(group="kalinka.plugins"):
        dist = getattr(ep, "dist", None)
        if dist is not None:
            plugins[dist.metadata["Name"]] = dist.version
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        os_name = platform.platform()
    return {
        "kalinka_version": get_version(),
        "plugin_versions": dict(sorted(plugins.items())),
        "os": os_name,
        "architecture": platform.machine(),
    }


def _manifest(
    parts: Sequence[ArchivePart],
    collected: Dict[str, _Collected],
    since: float,
    until: float,
    warnings: List[ExportWarning],
) -> bytes:
    sources = {}
    for part in parts:
        got = collected[part.source.name]
        sources[part.source.name] = {
            "entry": ENTRY_NAMES[part.source.name],
            **part.source.describe(),
            "readable": got.readable,
            "first_record": _iso(got.first),
            "last_record": _iso(got.last),
            "records": len(got.lines),
            "bytes": got.size_bytes,
            "records_withheld": got.withheld,
            "truncated": got.truncated,
            "time_filter_applied": got.time_filtered,
        }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        **describe_install(),
        "requested_range": {"since": _iso(since), "until": _iso(until)},
        "sources": sources,
        "warnings": [vars(w) for w in warnings],
    }
    return json.dumps(manifest, indent=2).encode("utf-8") + b"\n"


def _chunks(lines: List[bytes]) -> Iterator[bytes]:
    pending: List[bytes] = []
    size = 0
    for line in lines:
        pending.append(line)
        size += len(line)
        if size >= _ZIP_CHUNK_BYTES:
            yield b"".join(pending)
            pending, size = [], 0
    if pending:
        yield b"".join(pending)


def _write_zip(path: str, entries: Dict[str, List[bytes]], manifest: bytes, token: CancelToken) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, lines in entries.items():
            entry = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
            entry.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(entry, "w") as out:
                for chunk in _chunks(lines):
                    token.check()
                    out.write(chunk)
        archive.writestr("manifest.json", manifest)


def write_archive(
    target: str,
    parts: Sequence[ArchivePart],
    since: float,
    until: float,
    carries_credential: Callable[[str], bool],
    token: CancelToken,
) -> ArchiveResult:
    """Collect every part and publish the archive at ``target``.

    Nothing is left behind unless it succeeds: the partial file is removed
    whatever stops it.

    @raise ExportCancelled If the token is cancelled before the rename.
    @raise ExportFailure With ``logs_unreadable`` when a required source
        cannot be read, ``insufficient_storage`` when the disk is full, and
        ``export_failed`` for any other write error.
    """
    partial = target + ".part"
    collected: Dict[str, _Collected] = {}
    warnings: List[ExportWarning] = []
    try:
        for part in parts:
            name = part.source.name
            try:
                collected[name] = collect(part, since, until, carries_credential, token)
            except LogSourceUnreadable as e:
                if part.required:
                    raise ExportFailure("logs_unreadable") from e
                collected[name] = _Collected(readable=False)
            warnings.extend(warnings_for(name, collected[name], since))
        entries = {
            ENTRY_NAMES[part.source.name]: collected[part.source.name].lines
            for part in parts
        }
        _write_zip(partial, entries, _manifest(parts, collected, since, until, warnings), token)
        token.check()
        os.rename(partial, target)
    except BaseException as e:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
        if isinstance(e, OSError):
            no_room = e.errno in (errno.ENOSPC, errno.EDQUOT)
            raise ExportFailure("insufficient_storage" if no_room else "export_failed") from e
        raise
    return ArchiveResult(os.stat(target).st_size, warnings)
