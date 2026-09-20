"""The run's own stopwatch.

Every phase the runner drives is timed here, and the timings are written as
they happen — a run that dies in stage 3 still leaves the timeline of stages
1 and 2 behind.
"""

from __future__ import annotations

import csv
import json
import time
from contextlib import contextmanager
from pathlib import Path


class Clock:
    def __init__(self, path: Path):
        self._path = path
        self.spans: dict[str, float] = {}
        if path.exists():
            self.spans = json.loads(path.read_text())

    @contextmanager
    def span(self, name: str):
        started = time.time()
        try:
            yield
        finally:
            self.record(name, time.time() - started)

    def record(self, name: str, seconds: float) -> None:
        self.spans[name] = round(seconds, 3)
        self._path.write_text(json.dumps(self.spans, indent=2))

    def total(self, *names: str) -> float:
        return round(sum(self.spans.get(name, 0.0) for name in names), 3)

    def write_csv(self, path: Path, rows: list[tuple[str, float, str]], total: float) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["stage", "seconds", "percent_of_end_to_end", "group"])
            for name, seconds, group in rows:
                writer.writerow([
                    name, round(seconds, 2),
                    round(100.0 * seconds / total, 2) if total else 0.0,
                    group,
                ])
