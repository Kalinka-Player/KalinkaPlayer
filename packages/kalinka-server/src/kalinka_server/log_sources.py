"""Where an export reads the logs from: the journal, or a developer's file.

A source yields its records newest first, so a reader that stops at a size
cap keeps the most recent ones, and a slow scan of old history can be cut
short. Each source renders its records as readable text itself; nothing
downstream sees raw journal data.
"""

import json
import os
import re
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .journal_reader import SOURCE_UNITS
from .logging_setup import DATE_FORMAT

SERVER = "server"
LOCAL_RENDERER = "local_renderer"

READER_SOCKET = "/run/kalinka-journal-reader.sock"
RENDERER_UNIT_FILE = "/usr/lib/systemd/system/kalinka-renderer.service"

#: A record longer than this is cut, and says so.
MAX_RECORD_BYTES = 64 * 1024

_CONNECT_TIMEOUT_S = 5.0

# Room for a capped message after JSON escaping has grown it several times.
_MAX_READER_LINE = 1024 * 1024

_PRIORITY_NAMES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


class LogSourceUnreadable(Exception):
    """The source exists but its records could not be read."""


@dataclass(frozen=True)
class LogRecord:
    """One record, already rendered as text.

    ``timestamp`` is Unix seconds, or None for a record that carries none.
    """

    timestamp: Optional[float]
    text: str


class LogReading(ABC):
    """One pass over a source's records within a time range.

    Iterate :meth:`records` from one thread; :meth:`close` may be called from
    any other to unblock it, after which the iteration ends with
    :class:`LogSourceUnreadable`.
    """

    @abstractmethod
    def records(self) -> Iterator[LogRecord]:
        """Records in range, newest first.

        @raise LogSourceUnreadable If the records cannot be read, at the
            start or part way through.
        """

    @abstractmethod
    def close(self) -> None: ...

    @property
    def history_start(self) -> Optional[float]:
        """When the retained history begins, known once :meth:`records` is
        exhausted; None when unknown."""
        return None

    @property
    def time_filtered(self) -> bool:
        """Whether every record yielded was chosen by its timestamp."""
        return True


class LogSource(ABC):
    """A fixed set of logs rendered into one file of an export."""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def open(self, since: float, until: float) -> LogReading: ...

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """What the manifest records about where the records came from."""


def cap_record(text: str) -> str:
    """``text`` cut to :data:`MAX_RECORD_BYTES`, marked when it was."""
    data = text.encode("utf-8", "replace")
    if len(data) <= MAX_RECORD_BYTES:
        return text
    kept = data[:MAX_RECORD_BYTES].decode("utf-8", "ignore")
    return f"{kept} [cut: the record was {len(data)} bytes]"


def _iso_utc(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _journal_text(value: Any) -> str:
    # journalctl sends a non-UTF-8 field as a list of bytes, a repeated one as a list.
    if isinstance(value, list):
        if all(isinstance(v, int) for v in value):
            return bytes(value).decode("utf-8", "replace")
        return " ".join(_journal_text(v) for v in value)
    return "" if value is None else str(value)


def render_journal_record(entry: Mapping[str, Any]) -> LogRecord:
    """A journal JSON entry as one readable line: time, unit, level, message.

    The unit is the one the record is about — systemd's own messages about a
    unit are logged by PID 1, under ``UNIT`` rather than ``_SYSTEMD_UNIT``.
    """
    try:
        timestamp: Optional[float] = int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
    except (KeyError, TypeError, ValueError):
        timestamp = None
    unit = next(
        (
            _journal_text(entry[key])
            for key in ("UNIT", "OBJECT_SYSTEMD_UNIT", "COREDUMP_UNIT", "_SYSTEMD_UNIT")
            if entry.get(key)
        ),
        "-",
    )
    try:
        level = _PRIORITY_NAMES[int(_journal_text(entry.get("PRIORITY")))]
    except (ValueError, IndexError):
        level = "-"
    when = _iso_utc(timestamp) if timestamp is not None else "-"
    message = _journal_text(entry.get("MESSAGE"))
    return LogRecord(timestamp, cap_record(f"{when} {unit} [{level}] {message}"))


_REALTIME_RE = re.compile(rb'"__REALTIME_TIMESTAMP"\s*:\s*"(\d+)"')


class _ReaderReading(LogReading):
    def __init__(self, socket_path: str, source: str, since: float, until: float):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._closed = threading.Event()
        self._history_start: Optional[float] = None
        request = {"source": source, "since": int(since), "until": int(until)}
        # Bounded, since the deadline can only close a reading once it exists.
        self._sock.settimeout(_CONNECT_TIMEOUT_S)
        try:
            self._sock.connect(socket_path)
            self._sock.sendall(json.dumps(request).encode() + b"\n")
            self._sock.settimeout(None)
        except OSError as e:
            self._sock.close()
            raise LogSourceUnreadable("the journal reader is not reachable") from e

    def records(self) -> Iterator[LogRecord]:
        stream = self._sock.makefile("rb")
        try:
            while True:
                line = self._readline(stream)
                if not line:
                    raise LogSourceUnreadable("the journal reader stopped early")
                if not line.endswith(b"\n"):
                    yield self._oversized(line, stream)
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as e:
                    raise LogSourceUnreadable("the journal reader sent garbage") from e
                if not isinstance(entry, dict):
                    raise LogSourceUnreadable("the journal reader sent garbage")
                if "status" in entry:
                    if entry["status"] != "ok":
                        raise LogSourceUnreadable(f"journal reader: {entry.get('code')}")
                    start = entry.get("journal_start")
                    self._history_start = float(start) if start is not None else None
                    return
                yield render_journal_record(entry)
        finally:
            stream.close()
            self._sock.close()

    def _readline(self, stream) -> bytes:
        try:
            line = stream.readline(_MAX_READER_LINE)
        except OSError as e:
            raise LogSourceUnreadable("the journal reader connection failed") from e
        if self._closed.is_set():
            raise LogSourceUnreadable("reading was cancelled")
        return line

    def _oversized(self, head: bytes, stream) -> LogRecord:
        while True:
            rest = self._readline(stream)
            if not rest or rest.endswith(b"\n"):
                break
        found = _REALTIME_RE.search(head)
        timestamp = int(found.group(1)) / 1_000_000 if found else None
        when = _iso_utc(timestamp) if timestamp is not None else "-"
        return LogRecord(timestamp, f"{when} - [-] [a record too large to read]")

    def close(self) -> None:
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def history_start(self) -> Optional[float]:
        return self._history_start


class JournalLogSource(LogSource):
    """A source's units, read through the journal reader."""

    def __init__(self, name: str, socket_path: str = READER_SOCKET) -> None:
        super().__init__(name)
        self._socket_path = socket_path

    def open(self, since: float, until: float) -> LogReading:
        return _ReaderReading(self._socket_path, self.name, since, until)

    def describe(self) -> Dict[str, Any]:
        return {"backend": "journal", "units": list(SOURCE_UNITS[self.name])}


# The full log format: "2026-09-23 10:00:00.123 INFO ...", in local time.
_FILE_STAMP_RE = re.compile(rb"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(\d{3}) ")
# The journal format: an sd-daemon priority and no time of its own.
_FILE_PRIORITY_RE = re.compile(rb"^<[0-7]>")


def _file_timestamp(line: bytes) -> Optional[float]:
    found = _FILE_STAMP_RE.match(line)
    if found is None:
        return None
    try:
        local = time.strptime(found.group(1).decode(), DATE_FORMAT)
    except ValueError:
        return None
    return time.mktime(local) + int(found.group(2)) / 1000


class _FileReading(LogReading):
    def __init__(self, path: str, since: float, until: float):
        try:
            self._fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._end = os.fstat(self._fd).st_size
        except OSError as e:
            raise LogSourceUnreadable("the log file cannot be opened") from e
        self._since = since
        self._until = until
        self._closed = threading.Event()
        self._history_start: Optional[float] = None
        self._time_filtered = True

    def _lines_backwards(self) -> Iterator[bytes]:
        position = self._end
        partial = b""
        while position > 0:
            if self._closed.is_set():
                raise LogSourceUnreadable("reading was cancelled")
            size = min(64 * 1024, position)
            position -= size
            try:
                chunk = os.pread(self._fd, size, position)
            except OSError as e:
                raise LogSourceUnreadable("the log file cannot be read") from e
            lines = (chunk + partial).split(b"\n")
            partial = lines[0]
            for line in reversed(lines[1:]):
                yield line
        yield partial

    def records(self) -> Iterator[LogRecord]:
        """Records newest first, a traceback kept with the record it follows."""
        following: List[bytes] = []
        oldest: Optional[float] = None
        try:
            for line in self._lines_backwards():
                timestamp = _file_timestamp(line)
                if timestamp is None and not _FILE_PRIORITY_RE.match(line):
                    if line:
                        following.append(line)
                    continue
                lines, following = [line, *reversed(following)], []
                text = cap_record(b"\n".join(lines).decode("utf-8", "replace"))
                if timestamp is None:
                    self._time_filtered = False
                    yield LogRecord(None, text)
                    continue
                oldest = timestamp
                if timestamp >= self._until:
                    continue
                if timestamp < self._since:
                    return
                yield LogRecord(timestamp, text)
            self._history_start = oldest
        finally:
            os.close(self._fd)

    def close(self) -> None:
        self._closed.set()

    @property
    def history_start(self) -> Optional[float]:
        return self._history_start

    @property
    def time_filtered(self) -> bool:
        return self._time_filtered


class FileLogSource(LogSource):
    """The file the developer launcher tees the server's output into.

    Under ``KALINKA_LOG_FORMAT=journal`` its records carry no time, so they
    are taken newest first regardless of the range and the reading reports
    that it could not filter them.
    """

    def __init__(self, name: str, path: str) -> None:
        super().__init__(name)
        self._path = path

    def open(self, since: float, until: float) -> LogReading:
        return _FileReading(self._path, since, until)

    def describe(self) -> Dict[str, Any]:
        return {"backend": "file", "file": os.path.basename(self._path)}

    def exists(self) -> bool:
        return os.path.isfile(self._path)


class LogSourceCatalog(ABC):
    """The sources this install can export right now."""

    @abstractmethod
    def available(self) -> Dict[str, LogSource]: ...


class JournalCatalog(LogSourceCatalog):
    """Production: the journal, through the reader, and the renderer on this
    machine when its package is installed."""

    def __init__(
        self,
        socket_path: str = READER_SOCKET,
        renderer_unit_file: str = RENDERER_UNIT_FILE,
    ) -> None:
        self._socket_path = socket_path
        self._renderer_unit_file = renderer_unit_file

    def available(self) -> Dict[str, LogSource]:
        if not os.path.exists(self._socket_path):
            return {}
        sources: Dict[str, LogSource] = {
            SERVER: JournalLogSource(SERVER, self._socket_path)
        }
        if os.path.exists(self._renderer_unit_file):
            sources[LOCAL_RENDERER] = JournalLogSource(LOCAL_RENDERER, self._socket_path)
        return sources


class FileCatalog(LogSourceCatalog):
    """Development: the server's file, and never a renderer."""

    def __init__(self, path: str) -> None:
        self._source = FileLogSource(SERVER, path)

    def available(self) -> Dict[str, LogSource]:
        return {SERVER: self._source} if self._source.exists() else {}
