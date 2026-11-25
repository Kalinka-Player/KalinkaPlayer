"""New music arriving in a large, settled library.

The library's workers (indexer and enricher in one process, embedder,
searcher) write one SQLite file, and a writer that waits five seconds for the
lock gives up. A statement whose cost grows with the library holds that lock
for longer the more music there is, so what passes on ten tracks locks
everyone else out on twenty thousand: enrichment passes die, moved files
are dropped.

The real indexer builds a library of synthetic tracks, which is then marked
enriched and embedded. Two real albums arrive in it, and one is then split
into disc folders. Everything has to settle, no write may be refused, and a
probe polling for the write lock must never be kept from it for long. When
it is, the failure names the calls that were under way.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import call_log
from large_library import mark_settled, plant_synthetic_library
from lock_probe import WriteLockProbe
from recordings import ART_OF_FUGUE, GOLDBERG, PlantedTrack, plant
from server_instance import KalinkaInstance
from waiting import describe_stage, scan_finished, stage_settled, wait_until

TRACKS = int(os.environ.get("KALINKA_SYSTEM_TEST_LARGE_TRACKS", "19182"))

SETUP_TIMEOUT_S = 120.0
INDEX_TIMEOUT_S = 3600.0
SETTLE_TIMEOUT_S = 900.0
MOVE_TIMEOUT_S = 300.0

# Half of what a worker waits for the lock before it fails.
MAX_HOLD_S = 2.5

_LOCALFILES = "input_modules.localfiles"
_STAGES = ("enrichment", "clap_audio", "clap_text")


@dataclass(frozen=True)
class SettledLibrary:
    """The server over the settled library, as the albums arrive in it."""

    instance: KalinkaInstance
    probe: WriteLockProbe
    planted: list[PlantedTrack]
    planted_root: Path
    call_log: Path
    log_offset: int


def _settings(instance: KalinkaInstance, *, enricher: bool, ai_search: bool) -> dict:
    return {
        f"{_LOCALFILES}.music_folders": [str(instance.music_dir)],
        f"{_LOCALFILES}.daily_rescan": False,
        f"{_LOCALFILES}.enricher.enabled": enricher,
        f"{_LOCALFILES}.ai_search.enabled": ai_search,
    }


def _count(instance: KalinkaInstance, sql: str, params: tuple = ()) -> int:
    return instance.rows(sql, params)[0][0]


def _count_once_there(instance: KalinkaInstance, sql: str, params: tuple = ()) -> int:
    """A count to poll while the server is still creating its database: 0
    until the query can be answered."""
    try:
        return _count(instance, sql, params)
    except sqlite3.OperationalError:
        return 0


def _record_enrichment_setup(instance: KalinkaInstance) -> None:
    """Let the enricher see its setup once, over an empty library. It
    re-opens what it could not match whenever that setup is new to it, which
    here would be every synthetic track."""
    instance.start()
    wait_until(
        lambda: _count_once_there(
            instance,
            "SELECT COUNT(*) FROM enricher_state WHERE key = 'enrichment_fingerprint'",
        ),
        timeout=SETUP_TIMEOUT_S,
        what="the enricher to record its setup",
    )
    instance.stop()


def _index_synthetic_library(instance: KalinkaInstance, scratch: Path) -> None:
    plant_synthetic_library(instance.music_dir / "Synthetic", TRACKS, scratch)
    instance.configure(_settings(instance, enricher=False, ai_search=False))
    instance.start()

    def indexed() -> int:
        return _count_once_there(instance, "SELECT COUNT(*) FROM tracks")

    wait_until(
        lambda: indexed() >= TRACKS,
        timeout=INDEX_TIMEOUT_S,
        interval=10.0,
        what="the synthetic library to be indexed",
        progress=lambda: f"{indexed()}/{TRACKS} indexed",
    )
    wait_until(
        lambda: scan_finished(instance.indexer_status().get("indexing")),
        timeout=INDEX_TIMEOUT_S,
        what="the scan to finish",
    )
    instance.stop()


@pytest.fixture(scope="module")
def large(new_instance, workspace: Path, cache_dir: Path) -> SettledLibrary:
    instance = new_instance("large")
    instance.install(
        _settings(instance, enricher=True, ai_search=False), cache_dir / "models"
    )
    try:
        _record_enrichment_setup(instance)
        _index_synthetic_library(instance, workspace)
        mark_settled(instance.db_path)

        planted_root = instance.music_dir / "Planted"
        library_root = str(planted_root.resolve())
        planted = [
            *plant(GOLDBERG, cache_dir, planted_root, library_root),
            *plant(ART_OF_FUGUE, cache_dir, planted_root, library_root),
        ]
        instance.configure(_settings(instance, enricher=True, ai_search=True))
        calls = workspace / "large-calls.tsv"
        log_offset = instance.log_size()
        instance.start(env=call_log.environment(calls))
        probe = WriteLockProbe(instance.db_path)
        probe.start()
        yield SettledLibrary(instance, probe, planted, planted_root, calls, log_offset)
        probe.stop()
    finally:
        instance.stop()


def _all_settled(instance: KalinkaInstance) -> bool:
    status = instance.indexer_status()
    return all(stage_settled(status.get(stage)) for stage in _STAGES)


def _progress(instance: KalinkaInstance) -> str:
    status = instance.indexer_status()
    return "; ".join(
        f"{stage} {describe_stage(status.get(stage))}" for stage in _STAGES
    )


def _split_into_discs(folder: Path) -> set[str]:
    """Move an album's files into two disc folders beside it, each keeping
    its inode as a file manager's move does. Returns where they now are."""
    files = sorted(folder.iterdir())
    half = len(files) // 2
    moved = set()
    for disc, group in ((1, files[:half]), (2, files[half:])):
        target = folder.parent / f"{folder.name} (disc {disc})"
        target.mkdir()
        for path in group:
            os.rename(path, target / path.name)
            moved.add(str((target / path.name).resolve()))
    folder.rmdir()
    return moved


def test_a_restart_leaves_the_settled_library_settled(large):
    expected = {track.library_path for track in large.planted}
    wait_until(
        lambda: expected <= set(large.instance.tracks()),
        timeout=SETTLE_TIMEOUT_S,
        what="the planted albums to be indexed",
    )
    reopened = _count(
        large.instance,
        "SELECT COUNT(*) FROM tracks WHERE enriched = 0 AND file_path NOT LIKE ?",
        (f"{large.planted_root.resolve()}/%",),
    )
    assert reopened == 0


def test_the_new_albums_are_enriched_and_embedded(large):
    wait_until(
        lambda: _all_settled(large.instance),
        timeout=SETTLE_TIMEOUT_S,
        interval=5.0,
        what="enrichment and AI embedding to settle",
        progress=lambda: _progress(large.instance),
    )
    unenriched = _count(large.instance, "SELECT COUNT(*) FROM tracks WHERE enriched = 0")
    assert unenriched == 0


def test_an_album_split_into_disc_folders_is_followed(large):
    folder = large.planted_root / GOLDBERG.folder
    before = {
        row["id"]
        for path, row in large.instance.tracks().items()
        if path.startswith(f"{folder.resolve()}/")
    }
    moved = _split_into_discs(folder)
    wait_until(
        lambda: moved <= set(large.instance.tracks()) and _all_settled(large.instance),
        timeout=MOVE_TIMEOUT_S,
        interval=2.0,
        what="the moved files to be found again and everything to settle",
        progress=lambda: _progress(large.instance),
    )
    tracks = large.instance.tracks()
    assert {tracks[path]["id"] for path in moved} == before
    assert len(tracks) == TRACKS + len(large.planted)


def test_no_write_was_refused(large):
    refused = [
        line
        for line in large.instance.log_since(large.log_offset).splitlines()
        if "database is locked" in line
    ]
    assert not refused, "\n".join(refused[:20])


def test_the_write_lock_is_never_out_of_reach_for_long(large):
    calls = call_log.read(large.call_log)
    assert calls, "the call log is empty: the hook no longer sees the workers' calls"
    too_long = large.probe.holds(MAX_HOLD_S)
    assert not too_long, "\n".join(
        "out of reach for {:.1f}s from {}: {}".format(
            hold.seconds,
            time.strftime("%H:%M:%S", time.localtime(hold.started)),
            call_log.busy_during(calls, hold),
        )
        for hold in too_long
    )
