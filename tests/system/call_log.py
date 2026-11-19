"""The library-database calls the server's workers made, as the hook in
``call_log_hook`` recorded them, to say who held the write lock."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from lock_probe import Hold

_HOOK_DIR = Path(__file__).resolve().parent / "call_log_hook"


@dataclass(frozen=True)
class Call:
    """One library-database call a worker made, as the hook recorded it."""

    started: float
    seconds: float
    process: str
    method: str
    rows_changed: int
    error: str

    @property
    def ended(self) -> float:
        return self.started + self.seconds


def environment(log_path: Path) -> dict[str, str]:
    """What a server needs in its environment to record its calls in
    ``log_path``."""
    inherited = os.environ.get("PYTHONPATH")
    return {
        "PYTHONPATH": os.pathsep.join(filter(None, [str(_HOOK_DIR), inherited])),
        "KALINKA_CALL_LOG": str(log_path),
    }


def read(log_path: Path) -> list[Call]:
    """Every call recorded so far: those that changed rows, failed, or took
    half a second. A call that only read, quickly, is not recorded."""
    try:
        lines = log_path.read_text().splitlines()
    except OSError:
        return []
    calls = []
    for line in lines:
        started, seconds, process, method, changed, error = line.split("\t")
        calls.append(
            Call(float(started), float(seconds), process, method, int(changed), error)
        )
    return calls


def busy_during(calls: list[Call], hold: Hold) -> str:
    """The recorded calls under way while ``hold`` lasted, the one that spent
    longest inside it first. Calls that failed are left out: they were kept
    waiting, and held nothing."""
    inside: dict[str, list[float]] = defaultdict(list)
    for call in calls:
        overlap = min(call.ended, hold.ended) - max(call.started, hold.started)
        if not call.error and overlap > 0:
            inside[f"{call.process} {call.method}"].append(overlap)
    if not inside:
        return "no recorded call was under way"
    return "; ".join(
        f"{name} ({len(spans)} call(s), {sum(spans):.1f}s)"
        for name, spans in sorted(inside.items(), key=lambda item: -sum(item[1]))[:5]
    )
