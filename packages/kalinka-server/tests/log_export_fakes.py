"""Log sources that answer from memory, for the export's tests."""

import threading
from typing import Dict, Iterator, List, Optional

from kalinka_server.log_sources import (
    LogReading,
    LogRecord,
    LogSource,
    LogSourceCatalog,
    LogSourceUnreadable,
)


class ListReading(LogReading):
    def __init__(self, source: "ListSource") -> None:
        self._source = source
        self._closed = threading.Event()

    def records(self) -> Iterator[LogRecord]:
        source = self._source
        if source.fail:
            raise LogSourceUnreadable("told to fail")
        for record in sorted(source.records, key=lambda r: -(r.timestamp or 0)):
            if source.gate is not None:
                source.reached_gate.set()
                while not source.gate.wait(0.01):
                    if self._closed.is_set():
                        raise LogSourceUnreadable("closed")
            yield record

    def close(self) -> None:
        self._closed.set()

    @property
    def history_start(self) -> Optional[float]:
        return self._source.history_start

    @property
    def time_filtered(self) -> bool:
        return self._source.time_filtered


class ListSource(LogSource):
    """Yields ``records`` newest first.

    With a ``gate``, it stops before each record until the gate opens or the
    reading is closed, so a test can hold an export in preparation.
    """

    def __init__(
        self,
        name: str,
        records: List[LogRecord] = (),
        *,
        fail: bool = False,
        history_start: Optional[float] = None,
        time_filtered: bool = True,
        gate: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name)
        self.records = list(records)
        self.fail = fail
        self.history_start = history_start
        self.time_filtered = time_filtered
        self.gate = gate
        self.reached_gate = threading.Event()

    def open(self, since: float, until: float) -> LogReading:
        return ListReading(self)

    def describe(self) -> dict:
        return {"backend": "memory"}


class FixedCatalog(LogSourceCatalog):
    def __init__(self, *sources: LogSource) -> None:
        self.sources: Dict[str, LogSource] = {s.name: s for s in sources}

    def available(self) -> Dict[str, LogSource]:
        return dict(self.sources)


def records(*pairs) -> List[LogRecord]:
    """``(timestamp, text)`` pairs as records."""
    return [LogRecord(timestamp, text) for timestamp, text in pairs]
