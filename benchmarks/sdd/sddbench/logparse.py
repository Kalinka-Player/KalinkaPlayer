"""Reading the server's own log back as measurements.

Two kinds of line matter: the ones the server already writes (``CLAP audio
embedding: 0.412s``) and the ones the benchmark's wrappers add (``BENCH
embed_track t=... decode=...``). Both carry the full timestamp format, which
is what makes a stage boundary a time rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"(?P<level>\w+) (?P<thread>\d+) (?P<logger>[\w.-]+): (?P<message>.*)$"
)
_BENCH = re.compile(r"^BENCH (?P<event>\w+) (?P<fields>.*)$")


@dataclass(frozen=True)
class Entry:
    at: datetime
    logger: str
    message: str

    @property
    def epoch(self) -> float:
        return self.at.timestamp()


@dataclass(frozen=True)
class Bench(Entry):
    event: str
    fields: dict[str, str]

    def number(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.fields[key])
        except (KeyError, ValueError):
            return default


def entries(log_path: Path) -> Iterator[Entry]:
    """Every parseable line, in order. Unparseable lines (tracebacks, the
    launcher's own output) are skipped rather than guessed at."""
    with log_path.open(errors="replace") as handle:
        for line in handle:
            match = _LINE.match(line.rstrip("\n"))
            if not match:
                continue
            at = datetime.strptime(match["ts"], "%Y-%m-%d %H:%M:%S.%f")
            message = match["message"]
            bench = _BENCH.match(message)
            if bench:
                fields = dict(
                    pair.split("=", 1)
                    for pair in bench["fields"].split(" ")
                    if "=" in pair
                )
                yield Bench(at, match["logger"], message, bench["event"], fields)
            else:
                yield Entry(at, match["logger"], message)


def bench(log_path: Path, event: Optional[str] = None) -> list[Bench]:
    return [
        entry
        for entry in entries(log_path)
        if isinstance(entry, Bench) and (event is None or entry.event == event)
    ]


def by_event(log_path: Path) -> dict[str, list[Bench]]:
    """Every BENCH line grouped by event, in one pass over the log."""
    grouped: dict[str, list[Bench]] = {}
    for entry in entries(log_path):
        if isinstance(entry, Bench):
            grouped.setdefault(entry.event, []).append(entry)
    return grouped


def first(log_path: Path, needle: str) -> Optional[Entry]:
    for entry in entries(log_path):
        if needle in entry.message:
            return entry
    return None


def last(log_path: Path, needle: str) -> Optional[Entry]:
    found = None
    for entry in entries(log_path):
        if needle in entry.message:
            found = entry
    return found
