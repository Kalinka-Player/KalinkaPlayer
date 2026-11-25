"""The board probe reads /proc and the server's status the way the board
reports them, and its summaries add up."""

import board_probe as probe


def _process(proc, pid, name, utime=0, stime=0):
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "comm").write_text(name + "\n")
    rest = " ".join(["S", "1"] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 6)
    (entry / "stat").write_text(f"{pid} ({name}) {rest}\n")


def test_only_kalinkas_own_processes_are_followed(tmp_path):
    _process(tmp_path, 20, "kal-librarian")
    _process(tmp_path, 10, "kalinka-server")
    _process(tmp_path, 30, "sshd")
    (tmp_path / "self").mkdir()

    assert probe.kalinka_processes(tmp_path) == [
        probe.Process(10, "kalinka-server"),
        probe.Process(20, "kal-librarian"),
    ]


def test_a_process_that_exits_mid_listing_is_skipped(tmp_path):
    (tmp_path / "40").mkdir()

    assert probe.kalinka_processes(tmp_path) == []


def test_status_lines_are_read_in_kb():
    status = "Name:\tkalinka-server\nVmRSS:\t   81234 kB\nVmSwap:\t       0 kB\n"

    assert probe.field_kb(status, "VmRSS") == 81234
    assert probe.field_kb(status, "Pss") is None


def test_cpu_time_survives_a_name_with_parentheses():
    rest = " ".join(["S", "1"] + ["0"] * 9 + ["250", "50"] + ["0"] * 6)

    assert probe.cpu_seconds(f"7 (kal (x) y) {rest}", ticks=100) == 3.0


def test_cpu_is_summed_per_process_name(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "CLOCK_TICKS", 100)
    _process(tmp_path, 1, "kal-librarian", utime=100)
    _process(tmp_path, 2, "kal-librarian", stime=50)

    assert probe.process_cpu(tmp_path) == {"kal-librarian": 1.5}


def test_percentiles_take_the_nearest_rank():
    values = [5, 1, 4, 2, 3]

    assert probe.percentile(values, 50) == 3
    assert probe.percentile(values, 95) == 5
    assert probe.percentile([7], 95) == 7


def _status(scanning=False, pending=False, last=None):
    return {
        "localfiles": {
            "indexing": {
                "scanning": scanning,
                "scan_pending": pending,
                "last_scan_at": last,
            }
        }
    }


def test_a_rescan_is_done_once_a_newer_scan_ended():
    assert probe.scan_outcome(100.0, _status(scanning=True, last=100.0)) is None
    assert probe.scan_outcome(100.0, _status(pending=True, last=101.0)) is None
    assert probe.scan_outcome(100.0, _status(last=101.0)) is True
    assert probe.scan_outcome(None, _status(last=5.0)) is True


def test_a_rescan_that_ends_with_no_newer_scan_failed():
    assert probe.scan_outcome(100.0, _status(last=100.0)) is False
    assert probe.scan_outcome(None, _status()) is False


def test_the_indexing_rate_charges_each_track_its_share():
    samples = [(0.0, 100, 10.0), (30.0, 110, 25.0), (60.0, 130, 70.0)]

    assert probe.indexing_rate(samples) == {
        "tracks_added": 30,
        "wall_s": 60.0,
        "tracks_per_min": 30.0,
        "wall_s_per_track": 2.0,
        "librarian_cpu_s_per_track": 2.0,
    }


def test_a_run_that_added_nothing_reports_no_rate():
    summary = probe.indexing_rate([(0.0, 5, 1.0), (10.0, 5, 1.5)])

    assert summary["tracks_added"] == 0
    assert summary["wall_s_per_track"] is None
