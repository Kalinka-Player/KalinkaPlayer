"""Stage 2 — index the library through the server's own pipeline and time it.

Nothing here drives the pipeline: the server scans because a music folder was
configured, and embeds because the scan nudged its embedder. All this does is
wait on the server's own progress signal and read the clock afterwards.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import logparse
from .dataset import load_manifest
from .instance import KalinkaInstance, describe, settled, stage, wait_until
from .paths import Layout

SCAN_TIMEOUT_S = 1800.0
EMBED_TIMEOUT_S = 14400.0


@dataclass
class Span:
    """One stage of the run, as a wall-clock window."""

    name: str
    started: float
    ended: float

    @property
    def seconds(self) -> float:
        return self.ended - self.started


def index(
    instance: KalinkaInstance, layout: Layout, mood: bool = True, log=print
) -> dict:
    """Start the server over the prepared library and wait for the pipeline to
    report itself done. Returns the timings and the outcome counts."""
    spans: list[Span] = []

    started = time.time()
    instance.start()
    reachable = time.time()
    spans.append(Span("server_start", started, reachable))
    log(f"server up on {instance.base_url} after {reachable - started:.1f}s")

    wait_until(
        lambda: "indexing" in instance.indexer_status()
        or bool(instance.tracks()),
        timeout=SCAN_TIMEOUT_S,
        what="the first scan to begin",
    )
    wait_until(
        lambda: "indexing" not in instance.indexer_status(),
        timeout=SCAN_TIMEOUT_S,
        what="the scan to finish",
        interval=2.0,
        progress=lambda: describe(stage(instance, "indexing")),
    )
    scan_done = time.time()
    spans.append(Span("scan", reachable, scan_done))
    log(f"scan finished: {len(instance.tracks())} track(s)")

    wait_until(
        lambda: settled(stage(instance, "clap_audio")),
        timeout=EMBED_TIMEOUT_S,
        what="CLAP audio embedding to settle",
        interval=5.0,
        progress=lambda: describe(stage(instance, "clap_audio")),
    )
    embed_done = time.time()
    spans.append(Span("embedding", scan_done, embed_done))

    # The mood head runs off the stored vectors after the audio jobs drain;
    # it has no stage of its own in the status, so it is waited on by the
    # count of tracks that still lack a (V,A) pair.
    if mood:
        wait_until(
            lambda: _tracks_without_mood(instance) == 0,
            timeout=EMBED_TIMEOUT_S,
            what="mood (V,A) backfill to finish",
            interval=5.0,
            progress=lambda: f"{_tracks_without_mood(instance)} track(s) without (V,A)",
        )
    mood_done = time.time()
    spans.append(Span("mood_backfill", embed_done, mood_done))
    spans.append(Span("indexing_total", reachable, mood_done))

    status = instance.indexer_status()
    outcome = _outcome(instance, layout, status)
    timings = _timings(instance.log_path, layout)
    summary = {
        "spans": {span.name: round(span.seconds, 3) for span in spans},
        "stage_status": status,
        "outcome": outcome,
        "timings": timings,
    }
    layout.index_summary.write_text(json.dumps(summary, indent=2))
    return summary


def _tracks_without_mood(instance: KalinkaInstance) -> int:
    rows = instance.rows(
        "SELECT COUNT(*) AS n FROM tracks "
        "WHERE embedding_clap_audio IS NOT NULL AND mood_valence IS NULL"
    )
    return rows[0]["n"] if rows else 0


def _outcome(instance: KalinkaInstance, layout: Layout, status: dict) -> dict:
    """Every manifest track accounted for: indexed, embedded, or explained."""
    manifest = load_manifest(layout)
    indexed = instance.tracks()
    indexed_names = {Path(path).name for path in indexed}
    expected_names = {track.file for track in manifest.values()}
    embedded = {
        Path(row["file_path"]).name
        for row in instance.rows(
            "SELECT file_path FROM tracks t "
            "WHERE t.embedding_clap_audio IS NOT NULL"
        )
    }
    return {
        "manifest_tracks": len(manifest),
        "indexed": len(indexed_names & expected_names),
        "embedded": len(embedded & expected_names),
        "missing_from_index": sorted(expected_names - indexed_names),
        "indexed_but_not_embedded": sorted(expected_names - embedded),
        "clap_audio_failed": status.get("clap_audio", {}).get("failed", 0),
    }


def _timings(log_path: Path, layout: Layout) -> dict:
    """Per-track embedding timings from the log, written out per track and
    summarised. ``other`` is everything inside one embedding that is neither
    fragment decode nor ONNX inference: opening the stream, seeking, the
    int16 quantisation roundtrip and the generator's collection."""
    events = logparse.by_event(log_path)
    tracks = events.get("embed_track", [])
    rows = [
        {
            "n": index,
            "total_s": entry.number("t"),
            "decode_s": entry.number("decode"),
            "infer_s": entry.number("infer"),
            "other_s": entry.number("other"),
            "ok": int(entry.number("ok")),
        }
        for index, entry in enumerate(tracks, start=1)
    ]
    with layout.index_timings.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["n", "total_s", "decode_s", "infer_s", "other_s", "ok"]
        )
        writer.writeheader()
        writer.writerows(rows)

    def total(event: str, key: str = "t") -> float:
        return round(sum(e.number(key) for e in events.get(event, [])), 3)

    return {
        "embedded_tracks": len(rows),
        "embed_total_s": round(sum(r["total_s"] for r in rows), 3),
        "embed_decode_s": round(sum(r["decode_s"] for r in rows), 3),
        "embed_infer_s": round(sum(r["infer_s"] for r in rows), 3),
        "embed_other_s": round(sum(r["other_s"] for r in rows), 3),
        "embed_median_s": round(
            sorted(r["total_s"] for r in rows)[len(rows) // 2], 3
        ) if rows else 0.0,
        "db_write_embedding_s": total("db_write_embedding"),
        "db_claim_batch_s": total("db_claim_batch"),
        "db_schedule_jobs_s": total("db_schedule_jobs"),
        "db_write_mood_s": total("db_write_mood"),
        "va_head_s": total("va_head"),
        "va_backfill_s": total("va_backfill"),
        "scan_listing_s": total("scan_listing"),
        "scan_index_files_s": total("scan_index_files"),
        "scan_cleanup_s": total("scan_cleanup"),
        "model_load_audio_s": total("model_load_audio"),
        "model_load_text_s": total("model_load_text"),
    }
