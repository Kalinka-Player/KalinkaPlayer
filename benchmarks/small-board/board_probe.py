#!/usr/bin/env python3
"""What Kalinka costs on a small board: memory, start-up, API latency,
indexing and playback.

Copy this one file to the board and run it with the system Python: it needs
only the standard library and a running Kalinka. Run it as root, since the
library database and the per-process memory accounting belong to the service
user. Each command prints a summary and appends JSON lines to the results
file, which is what to send back.

    sudo python3 board_probe.py memory
    sudo systemctl restart kalinka && sudo python3 board_probe.py ready
    sudo python3 board_probe.py index      # while the first scan runs
    sudo python3 board_probe.py rescan
    sudo python3 board_probe.py api --query "dark side"
    sudo python3 board_probe.py cpu        # idle, then again while a track plays

It writes nothing but the results file. ``rescan`` asks for the same scan as
the app's "Rescan library".
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

PROC = Path("/proc")
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
DEFAULT_SERVER = "http://127.0.0.1:8000"
DEFAULT_DB = "/var/lib/kalinka/localfiles.db"
# A Pi 1 answers slowly while it indexes; a timeout is a result, not a hang.
REQUEST_TIMEOUT_S = 120


@dataclass(frozen=True)
class Process:
    pid: int
    name: str


def kalinka_processes(proc: Path = PROC) -> list[Process]:
    """The server, its workers, the renderer and the supervisor, by the names
    they give themselves (kalinka-*, kal-*)."""
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if name.startswith("kal"):
            found.append(Process(int(entry.name), name))
    return sorted(found, key=lambda p: p.pid)


def field_kb(text: str, key: str) -> Optional[int]:
    """The value of a ``Key:  123 kB`` line in a /proc status-style file."""
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name == key and value.split():
            return int(value.split()[0])
    return None


def cpu_seconds(stat: str, ticks: int = CLOCK_TICKS) -> float:
    """User plus system time from a /proc/<pid>/stat line, whose second field
    is a name that may itself hold spaces and parentheses."""
    fields = stat.rpartition(")")[2].split()
    return (int(fields[11]) + int(fields[12])) / ticks


def process_cpu(proc: Path = PROC) -> dict[str, float]:
    """CPU seconds so far per Kalinka process name, children of one name summed."""
    totals: dict[str, float] = {}
    for process in kalinka_processes(proc):
        try:
            stat = (proc / str(process.pid) / "stat").read_text()
        except OSError:
            continue
        totals[process.name] = totals.get(process.name, 0.0) + cpu_seconds(
            stat, CLOCK_TICKS
        )
    return totals


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, which a short run reports honestly."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))
    return ordered[int(rank) - 1]


def scan_outcome(before: Optional[float], status: dict) -> Optional[bool]:
    """None while the library's scan requested after ``before`` is pending or
    running, then whether it completed.

    The request keeps one of the two set until its scan ends, so an idle
    library with no newer scan means the scan failed, or the library is gone.
    """
    indexing = status.get("localfiles", {}).get("indexing", {})
    if indexing.get("scanning") or indexing.get("scan_pending"):
        return None
    last = indexing.get("last_scan_at")
    return last is not None and (before is None or last > before)


def indexing_rate(samples: Sequence[tuple[float, int, float]]) -> dict:
    """Tracks added, and what each cost, from (wall s, tracks, librarian CPU s)
    samples taken while a scan ran."""
    (t0, n0, c0), (t1, n1, c1) = samples[0], samples[-1]
    added = n1 - n0
    return {
        "tracks_added": added,
        "wall_s": round(t1 - t0, 1),
        "tracks_per_min": round(added / (t1 - t0) * 60, 1) if t1 > t0 else None,
        "wall_s_per_track": round((t1 - t0) / added, 2) if added else None,
        "librarian_cpu_s_per_track": round((c1 - c0) / added, 2) if added else None,
    }


class Results:
    """Appends one JSON object per measurement, each tagged with the board."""

    def __init__(self, path: Path):
        self._path = path
        self._board = board()

    def record(self, kind: str, **values) -> None:
        line = {"kind": kind, "at": round(time.time(), 1), **self._board, **values}
        with self._path.open("a") as out:
            out.write(json.dumps(line) + "\n")


def board(proc: Path = PROC) -> dict:
    model = Path("/proc/device-tree/model")
    meminfo = (proc / "meminfo").read_text()
    return {
        "model": model.read_text().strip("\0\n") if model.exists() else platform.node(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "mem_total_kb": field_kb(meminfo, "MemTotal"),
    }


def get_json(server: str, path: str, method: str = "GET") -> tuple[float, dict]:
    """The response and the seconds it took."""
    request = urllib.request.Request(server + path, method=method)
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        body = response.read()
    return time.monotonic() - started, json.loads(body or b"{}")


def library_tracks(db: str) -> int:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]


def librarian_cpu() -> float:
    return process_cpu().get("kal-librarian", 0.0)


def cmd_memory(args, results: Results) -> None:
    for process in kalinka_processes():
        base = PROC / str(process.pid)
        try:
            status = (base / "status").read_text()
            rollup = (base / "smaps_rollup").read_text()
        except OSError:
            continue
        row = {
            "process": process.name,
            "pid": process.pid,
            "rss_kb": field_kb(status, "VmRSS"),
            "pss_kb": field_kb(rollup, "Pss"),
            "swap_kb": field_kb(rollup, "Swap"),
        }
        results.record("memory", **row)
        print(f"{process.name:16} {process.pid:>7}  RSS {row['rss_kb']:>7} kB"
              f"  PSS {row['pss_kb']:>7} kB  swap {row['swap_kb']:>6} kB")
    meminfo = (PROC / "meminfo").read_text()
    system = {
        key: field_kb(meminfo, key)
        for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
    }
    results.record("system_memory", **system)
    print("system  " + "  ".join(f"{k} {v} kB" for k, v in system.items()))


def cmd_ready(args, results: Results) -> None:
    started = time.monotonic()
    while True:
        try:
            get_json(args.server, "/indexer/status")
            break
        except OSError:
            time.sleep(1)
    waited = time.monotonic() - started
    results.record("ready", seconds=round(waited, 1))
    print(f"server answered after {waited:.1f} s")


def cmd_api(args, results: Results) -> None:
    query = urllib.parse.quote(args.query)
    endpoints = {
        "indexer_status": "/indexer/status",
        "queue_state": "/queue/state",
        "search": f"/search/matches?query={query}&sources=localfiles",
    }
    for name, path in endpoints.items():
        latencies = [get_json(args.server, path)[0] for _ in range(args.runs)]
        summary = {
            "endpoint": name,
            "runs": args.runs,
            "median_ms": round(percentile(latencies, 50) * 1e3),
            "p95_ms": round(percentile(latencies, 95) * 1e3),
        }
        results.record("api", **summary)
        print(f"{name:16} median {summary['median_ms']:>6} ms"
              f"  p95 {summary['p95_ms']:>6} ms")


def cmd_index(args, results: Results) -> None:
    samples = [(time.monotonic(), library_tracks(args.db), librarian_cpu())]
    unchanged_since = samples[0][0]
    while time.monotonic() - unchanged_since < args.idle:
        time.sleep(args.interval)
        sample = (time.monotonic(), library_tracks(args.db), librarian_cpu())
        if sample[1] != samples[-1][1]:
            unchanged_since = sample[0]
            print(f"{sample[1]:>7} tracks  librarian CPU {sample[2]:.0f} s", flush=True)
        samples.append(sample)
    growing = [s for s in samples if s[0] <= unchanged_since]
    summary = indexing_rate(growing if len(growing) > 1 else samples)
    results.record("index", **summary)
    print(json.dumps(summary, indent=2))


def cmd_rescan(args, results: Results) -> None:
    _, status = get_json(args.server, "/indexer/status")
    before = status.get("localfiles", {}).get("indexing", {}).get("last_scan_at")
    cpu_before, started = librarian_cpu(), time.monotonic()
    get_json(args.server, "/indexer/rescan", method="POST")
    completed = None
    while completed is None:
        time.sleep(2)
        completed = scan_outcome(before, get_json(args.server, "/indexer/status")[1])
    if not completed:
        sys.exit("the rescan stopped without completing; the server's log says why")
    summary = {
        "tracks": library_tracks(args.db),
        "wall_s": round(time.monotonic() - started, 1),
        "librarian_cpu_s": round(librarian_cpu() - cpu_before, 1),
    }
    results.record("rescan", **summary)
    print(json.dumps(summary, indent=2))


def cmd_cpu(args, results: Results) -> None:
    before, started = process_cpu(), time.monotonic()
    time.sleep(args.seconds)
    after, wall = process_cpu(), time.monotonic() - started
    for name in sorted(after):
        percent = (after[name] - before.get(name, 0.0)) / wall * 100
        results.record("cpu", process=name, seconds=args.seconds,
                       percent_of_core=round(percent, 1), label=args.label)
        print(f"{name:16} {percent:5.1f} % of a core")


COMMANDS: dict[str, Callable] = {
    "memory": cmd_memory,
    "ready": cmd_ready,
    "api": cmd_api,
    "index": cmd_index,
    "rescan": cmd_rescan,
    "cpu": cmd_cpu,
}


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=Path("kalinka-probe.jsonl"))
    parser.add_argument("--query", default="love", help="api: the name searched")
    parser.add_argument("--runs", type=int, default=10, help="api: requests each")
    parser.add_argument("--interval", type=float, default=10, help="index: s between samples")
    parser.add_argument("--idle", type=float, default=300,
                        help="index: s without a new track that ends the run")
    parser.add_argument("--seconds", type=float, default=60, help="cpu: window")
    parser.add_argument("--label", default="", help="cpu: e.g. idle, flac-16-44")
    args = parser.parse_args(argv)
    COMMANDS[args.command](args, Results(args.out))


if __name__ == "__main__":
    main()
