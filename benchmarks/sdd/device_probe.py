#!/usr/bin/env python3
"""Where a CLAP embedding's time goes on the machine that will play the music.

Copy this one file to the device, point it at a CLAP model directory and a
folder of tracks, and it splits a track's embedding into file I/O, decode and
resample, ONNX inference, and everything else — through the shipped embedder's
own code path, not a reimplementation of it. The localfiles plugin has to be
importable; nothing else from this repository is needed, which is what lets it
run on a Raspberry Pi with only the release installed.

Two things it is careful about, because both were measured wrong first:

- Page cache. Each track is embedded once with its cache dropped and once with
  it hot, so the cold number is the one a first indexing pass actually pays.
  Reading from a tmpfs (``/tmp`` on Raspberry Pi OS) measures RAM, not storage.
- Heat. A passively cooled board slows down as the run goes on, so locations
  are visited round-robin and each measurement records the die temperature and
  the CPU clock. A straight sweep charges the drift to whatever ran last.

Nothing the server owns is touched: its own process, its own model directory,
read-only on the audio.
"""

from __future__ import annotations

import argparse
import csv
import gc
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass
class Buckets:
    """The stopwatches, accumulated over one embedding."""

    io_s: float = 0.0
    io_bytes: int = 0
    decode_s: float = 0.0
    decode_io_s: float = 0.0
    infer_s: float = 0.0

    def reset(self) -> None:
        self.io_s = self.decode_s = self.decode_io_s = self.infer_s = 0.0
        self.io_bytes = 0


class TimedStream:
    """A read-only view of an open file that charges its own time to *buckets*.

    soundfile drives the encoder's reads through this, so seek and read time
    lands in the I/O bucket instead of inflating the decode measurement.
    """

    def __init__(self, handle, buckets: Buckets) -> None:
        self._handle = handle
        self._buckets = buckets

    def read(self, size: int = -1) -> bytes:
        start = time.perf_counter()
        data = self._handle.read(size)
        self._buckets.io_s += time.perf_counter() - start
        self._buckets.io_bytes += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        start = time.perf_counter()
        position = self._handle.seek(offset, whence)
        self._buckets.io_s += time.perf_counter() - start
        return position

    def tell(self) -> int:
        return self._handle.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._handle.close()


@dataclass(frozen=True)
class Split:
    """One embedding, taken apart."""

    round: int
    location: str
    cache: str
    track: str
    total_s: float
    io_s: float
    decode_s: float
    infer_s: float
    other_s: float
    read_bytes: int
    temp_c: float
    clock_mhz: float


def evict(path: Path) -> None:
    """Drop this file's page cache, so the next read is the one a first
    indexing pass pays rather than a repeat of it."""
    handle = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(handle, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(handle)


def temperature_c() -> float:
    """SoC temperature, or 0.0 where the kernel does not report one."""
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            return int(zone.read_text()) / 1000.0
        except (OSError, ValueError):
            continue
    return 0.0


def vcgencmd(*arguments: str) -> str:
    """What the Raspberry Pi firmware says, or "" on anything else."""
    try:
        finished = subprocess.run(
            ["vcgencmd", *arguments], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return finished.stdout.strip() if finished.returncode == 0 else ""


def parse_clock(text: str) -> float:
    """``frequency(48)=1800404352`` in Hz, or sysfs' kHz, as MHz."""
    _, separator, value = text.partition("=")
    try:
        number = int(value.strip() if separator else text.strip())
    except ValueError:
        return 0.0
    return number / 1e6 if separator else number / 1e3


def clock_mhz() -> float:
    """What the first core is actually running at.

    On a Raspberry Pi the firmware throttles underneath Linux and
    ``scaling_cur_freq`` keeps reporting the frequency that was *asked* for, so
    a throttled board reads as a full-speed one. The firmware's own answer is
    the true one where it exists.
    """
    firmware = vcgencmd("measure_clock", "arm")
    if firmware:
        return parse_clock(firmware)
    try:
        return parse_clock(
            Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read_text()
        )
    except OSError:
        return 0.0


def throttling() -> str:
    """The firmware's throttling word, e.g. ``0xe0008``; "" where there is
    none. Bit 3 means it is throttling now, bits 16-19 that it has since
    boot."""
    return vcgencmd("get_throttled").partition("=")[2]


def state() -> str:
    """What the board is doing, printed either side of a run."""
    flags = throttling()
    return f"{temperature_c():.1f}C, {clock_mhz():.0f} MHz" + (
        f", throttled={flags}" if flags else ""
    )


def mount_of(path: Path) -> str:
    """The device and filesystem a location sits on — the check that catches a
    'disk' measurement that was really tmpfs."""
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return "unknown"
    target = str(path.resolve())
    device = fstype = point = ""
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        candidate = fields[1]
        under = target == candidate or target.startswith(candidate.rstrip("/") + "/")
        if under and len(candidate) >= len(point):
            device, point, fstype = fields[0], candidate, fields[2]
    return f"{device} ({fstype})" if device else "unknown"


def with_intra_op(factory, threads: int):
    """The shipped session options with one field changed.

    A sweep must differ from production in exactly the setting under test, so
    this asks the model for its own options and overrides the thread count
    rather than building a fresh SessionOptions and guessing the rest.
    """

    def options():
        chosen = factory()
        chosen.intra_op_num_threads = threads
        return chosen

    return options


class Probe:
    """The shipped audio encoder with three stopwatches inside it.

    Wraps the plugin's fragment loader and the ONNX session it runs, both by
    assignment, so the code under measurement is the installed one. The
    wrappers add timing and change nothing else. Use it as a context manager:
    on the way out it puts the fragment loader back and drops the session, so
    a second probe measures its own work and two 300 MB sessions never coexist
    on a small board.

    @param intra_op Overrides the audio session's thread count, for answering
        what the shipped setting costs. Left alone, the session is the one the
        server would have built.
    @param clap The plugin module, injected only by the tests.
    """

    def __init__(self, model_dir: Path, intra_op: int | None = None, clap=None) -> None:
        if clap is None:
            from kalinka_plugin_localfiles.embedder import clap_onnx as clap

        self.buckets = Buckets()
        self._clap = clap
        self._original_fragment = clap._read_fragment
        clap._read_fragment = self._timed_fragment(clap._read_fragment)
        self._model = clap.ClapOnnxModel(str(model_dir))
        if intra_op is not None:
            self._model._session_options = with_intra_op(
                self._model._session_options, intra_op
            )
        start = time.perf_counter()
        self._model.load_audio()
        self.load_s = time.perf_counter() - start
        if not self._model.is_audio_loaded:
            raise SystemExit(f"the audio encoder in {model_dir} did not load")
        self._model._audio_session.run = self._timed_infer(
            self._model._audio_session.run
        )
        self.intra_op = self._model._session_options().intra_op_num_threads

    def __enter__(self) -> "Probe":
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    def close(self) -> None:
        self._clap._read_fragment = self._original_fragment
        self._model = None
        gc.collect()

    def _timed_fragment(self, func):
        def wrapper(*args, **kwargs):
            io_before = self.buckets.io_s
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                self.buckets.decode_s += time.perf_counter() - start
                self.buckets.decode_io_s += self.buckets.io_s - io_before

        return wrapper

    def _timed_infer(self, func):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                self.buckets.infer_s += time.perf_counter() - start

        return wrapper

    def embed(self, path: Path, cache: str, number: int, location: str) -> Split:
        self.buckets.reset()
        if cache == "cold":
            evict(path)
        start = time.perf_counter()
        with path.open("rb") as handle:
            vector = self._model.get_audio_embedding(TimedStream(handle, self.buckets))
        total = time.perf_counter() - start
        if vector is None:
            raise SystemExit(f"no embedding came back for {path}")
        decode = self.buckets.decode_s - self.buckets.decode_io_s
        return Split(
            round=number,
            location=location,
            cache=cache,
            track=path.name,
            total_s=total,
            io_s=self.buckets.io_s,
            decode_s=decode,
            infer_s=self.buckets.infer_s,
            other_s=max(total - self.buckets.io_s - decode - self.buckets.infer_s, 0.0),
            read_bytes=self.buckets.io_bytes,
            temp_c=temperature_c(),
            clock_mhz=clock_mhz(),
        )


def sequential_read(path: Path) -> tuple[float, int]:
    """Cold full-file read, for context on what the medium can do at all."""
    evict(path)
    size = 0
    start = time.perf_counter()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 16):
            size += len(chunk)
    return time.perf_counter() - start, size


def median_split(splits: Sequence[Split]) -> dict[str, float]:
    return {
        "total": statistics.median(s.total_s for s in splits),
        "io": statistics.median(s.io_s for s in splits),
        "decode": statistics.median(s.decode_s for s in splits),
        "infer": statistics.median(s.infer_s for s in splits),
        "other": statistics.median(s.other_s for s in splits),
    }


def drift(splits: Sequence[Split]) -> tuple[float, float, float, float]:
    """How much slower, and how much hotter, the last round was than the first.

    Returns first and last median seconds and the two temperatures. On a board
    with headroom these are the same number twice; where they are not, the
    device throttles under sustained indexing and the first-round figure is a
    best case rather than a rate.
    """
    rounds = sorted({s.round for s in splits})
    first = [s for s in splits if s.round == rounds[0]]
    last = [s for s in splits if s.round == rounds[-1]]
    return (
        statistics.median(s.total_s for s in first),
        statistics.median(s.total_s for s in last),
        statistics.median(s.temp_c for s in first),
        statistics.median(s.temp_c for s in last),
    )


def audio_in(directory: Path) -> list[Path]:
    """The tracks to profile: MP3s where there are any, otherwise whatever
    files the folder holds, so a FLAC library profiles without a flag."""
    found = sorted(directory.glob("*.mp3"))
    return found or sorted(p for p in directory.glob("*") if p.is_file())


def locations(arguments: Iterable[str]) -> list[tuple[str, Path]]:
    """``label=/path`` pairs, or a bare path labelled by its directory name."""
    parsed = []
    for argument in arguments:
        label, _, directory = argument.rpartition("=")
        path = Path(directory).expanduser()
        parsed.append((label or path.name, path))
    return parsed


def write_csv(path: Path, splits: Sequence[Split]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "round", "location", "cache", "track", "total_s", "io_s",
                "decode_s", "infer_s", "other_s", "read_bytes", "temp_c",
                "clock_mhz",
            ]
        )
        for split in splits:
            writer.writerow(
                [
                    split.round, split.location, split.cache, split.track,
                    f"{split.total_s:.4f}", f"{split.io_s:.4f}",
                    f"{split.decode_s:.4f}", f"{split.infer_s:.4f}",
                    f"{split.other_s:.4f}", split.read_bytes,
                    f"{split.temp_c:.1f}", f"{split.clock_mhz:.0f}",
                ]
            )


def measure(probe: Probe, places, tracks, rounds: int) -> list[Split]:
    """Every location once per round, cold then warm, printing as it goes."""
    print()
    print(
        f"{'round':>5} {'location':<8} {'cache':<5} {'track':<12} {'total':>8} "
        f"{'io':>8} {'decode':>8} {'infer':>8} {'other':>8} {'temp':>6} {'MHz':>6}"
    )
    splits: list[Split] = []
    for number in range(1, rounds + 1):
        for cache in ("cold", "warm"):
            for label, _ in places:
                for track in tracks[label]:
                    split = probe.embed(track, cache, number, label)
                    splits.append(split)
                    print(
                        f"{split.round:>5} {split.location:<8} {split.cache:<5} "
                        f"{split.track[:12]:<12} {split.total_s:>7.3f}s "
                        f"{split.io_s:>7.3f}s {split.decode_s:>7.3f}s "
                        f"{split.infer_s:>7.3f}s {split.other_s:>7.3f}s "
                        f"{split.temp_c:>5.1f}C {split.clock_mhz:>6.0f}"
                    )
    return splits


def report(splits: Sequence[Split], places, rounds: int) -> None:
    summaries = {
        (label, cache): median_split(
            [s for s in splits if s.location == label and s.cache == cache]
        )
        for label, _ in places
        for cache in ("cold", "warm")
    }

    print()
    print(
        f"{'location':<10} {'cache':<6} {'median':>9} {'io':>8} "
        f"{'decode':>8} {'infer':>8} {'other':>8}"
    )
    for (label, cache), summary in summaries.items():
        print(
            f"{label:<10} {cache:<6} {summary['total']:>8.3f}s "
            f"{summary['io']:>7.3f}s {summary['decode']:>7.3f}s "
            f"{summary['infer']:>7.3f}s {summary['other']:>7.3f}s"
        )

    print(
        f"\n{'location':<10} {'cache':<6} {'io':>8} {'decode':>8} "
        f"{'infer':>8} {'other':>8}"
    )
    for (label, cache), summary in summaries.items():
        share = 100.0 / summary["total"] if summary["total"] else 0.0
        print(
            f"{label:<10} {cache:<6} {summary['io'] * share:>7.1f}% "
            f"{summary['decode'] * share:>7.1f}% {summary['infer'] * share:>7.1f}% "
            f"{summary['other'] * share:>7.1f}%"
        )

    first, last, cool, hot = drift(splits)
    print()
    print(
        f"drift: round 1 {first:.3f}s at {cool:.1f}C -> round {rounds} "
        f"{last:.3f}s at {hot:.1f}C ({(last / first - 1.0) * 100.0:+.0f}%)"
    )
    if last > first * 1.05:
        print(
            "        the device slows down under sustained embedding; plan "
            f"indexing at {last:.1f}s per track, not {first:.1f}s"
        )


def sweep(model_dir: Path, track: Path, values: Sequence[int], shipped: int, rounds: int) -> None:
    """What the audio session's thread count is worth on this device.

    One model load per value, so the values are visited in order on a board
    that is warming up — read the shape, not the last digit, and read a gain
    at a higher thread count as a floor rather than a ceiling.

    The shipped setting is always one of them, whether or not it was asked
    for: the column is a ratio against it, and a ratio against some other
    value under that heading would read as the opposite of what it is.
    """
    values = list(values) if shipped in values else [*values, shipped]
    print(f"\nintra_op_num_threads sweep, warm cache, {track.name[:12]}")
    measured: dict[int, float] = {}
    for value in values:
        with Probe(model_dir, intra_op=value) as probe:
            measured[value] = statistics.median(
                probe.embed(track, "warm", number, f"intra{value}").total_s
                for number in range(1, rounds + 1)
            )
    baseline = measured[shipped]
    print(f"{'threads':>7} {'median':>9} {'vs shipped':>11}")
    for value, median in measured.items():
        print(
            f"{value:>7} {median:>8.3f}s {median / baseline:>10.2f}x"
            + ("  (shipped)" if value == shipped else "")
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split a CLAP embedding into I/O, decode and inference on this device."
    )
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument(
        "location",
        nargs="+",
        help="a folder of audio, optionally labelled: label=/mnt/usb/Test",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--tracks", type=int, default=3, help="tracks per location")
    parser.add_argument("--csv", type=Path, help="write every measurement here")
    parser.add_argument(
        "--threads",
        help="also sweep the audio session's intra_op_num_threads, e.g. 1,2,3,4",
    )
    args = parser.parse_args(argv)
    # A run takes minutes on the hardware this is for, and its output is
    # usually redirected to a log someone is watching.
    sys.stdout.reconfigure(line_buffering=True)

    places = locations(args.location)
    tracks = {
        label: audio_in(path)[: args.tracks] for label, path in places
    }
    missing = [label for label, found in tracks.items() if not found]
    if missing:
        parser.error(f"no audio found in: {', '.join(missing)}")

    print(f"host      {platform.platform()}, {os.cpu_count()} cores")
    print(f"python    {sys.version.split()[0]}")
    print(f"model     {args.model_dir} on {mount_of(args.model_dir)}")
    for label, path in places:
        print(f"location  {label}: {path} on {mount_of(path)}")

    for label, found in tracks.items():
        for track in found:
            elapsed, size = sequential_read(track)
            print(
                f"{label:<8} {track.name[:12]:<12} cold sequential read "
                f"{size / 1e6:.2f} MB in {elapsed:.3f}s "
                f"({size / 1e6 / elapsed:.1f} MB/s)"
            )

    with Probe(args.model_dir) as probe:
        print(f"\naudio tower load: {probe.load_s:.2f}s, "
              f"intra_op_num_threads {probe.intra_op}")
        print(f"before: {state()}")
        shipped = probe.intra_op
        splits = measure(probe, places, tracks, args.rounds)

    print(f"after:  {state()}")

    report(splits, places, args.rounds)

    if args.threads:
        first_label = places[0][0]
        sweep(
            args.model_dir,
            tracks[first_label][0],
            [int(value) for value in args.threads.split(",")],
            shipped,
            args.rounds,
        )

    if args.csv:
        write_csv(args.csv, splits)
        print(f"\nwrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
