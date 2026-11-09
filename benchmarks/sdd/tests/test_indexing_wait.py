"""Scan completion stays observable after the indexing stage becomes idle."""

from types import SimpleNamespace

import pytest

from sddbench import indexing
from sddbench.paths import Layout
import waiting


def _scan(**changes):
    return {
        "total": 0,
        "done": 0,
        "pending": 0,
        "in_progress": 0,
        "failed": 0,
        "available": True,
        "scanning": False,
        "scan_pending": False,
        "last_scan_at": 123.5,
        **changes,
    }


@pytest.mark.parametrize(
    "before_completion",
    [
        [],
        [
            None,
            _scan(available=False),
            _scan(last_scan_at=None),
            _scan(scanning=True, last_scan_at=None),
            _scan(scan_pending=True),
            _scan(scanning=True),
        ],
    ],
    ids=["completed-before-first-poll", "startup-and-rescan"],
)
def test_index_waits_for_a_completed_scan_with_no_work_queued(
    tmp_path, monkeypatch, before_completion
):
    now = 0.0

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(
        waiting, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep)
    )
    monkeypatch.setattr(indexing, "SCAN_TIMEOUT_S", 30.0)

    class Instance:
        base_url = "http://127.0.0.1:1"
        log_path = tmp_path / "server.log"

        def __init__(self):
            self.remaining = list(before_completion)
            self.finished = False

        def start(self):
            self.log_path.touch()

        def indexer_status(self):
            self.finished = not self.remaining
            scan = self.remaining.pop(0) if self.remaining else _scan()
            return {
                **({"indexing": scan} if scan is not None else {}),
                "clap_audio": {"total": 1, "done": 1, "pending": 0, "in_progress": 0},
            }

        def tracks(self):
            assert self.finished, "the library was read before the scan finished"
            return {"/music/one.flac": {"id": "track-1"}}

        def rows(self, query):
            return [{"file_path": "/music/one.flac"}]

    layout = Layout(out=tmp_path)
    layout.manifest.write_text(
        "track_id,file,duration_s,bytes,n_captions,source_path,kalinka_track_id\n"
        "track-1,one.flac,120.0,100,1,one.flac,track-1\n"
    )
    summary = indexing.index(Instance(), layout, mood=False, log=lambda _: None)

    assert summary["stage_status"]["indexing"] == _scan()
    assert summary["outcome"]["indexed"] == 1
    assert summary["outcome"]["embedded"] == 1
    assert layout.index_summary.exists()
