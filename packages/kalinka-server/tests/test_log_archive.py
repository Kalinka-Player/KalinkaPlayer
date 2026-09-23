"""The archive an export publishes: what goes in, what is left out, and what
the manifest says about it."""

import errno
import json
import os
import stat
import threading
import zipfile

import pytest

from kalinka_server import log_archive
from kalinka_server.config_secrets import credential_detector
from kalinka_server.log_archive import (
    ArchivePart,
    CancelToken,
    ExportCancelled,
    ExportFailure,
    write_archive,
)
from tests.log_export_fakes import ListSource, records

T0 = 1_790_000_000.0
SINCE, UNTIL = T0, T0 + 3600


def _codes(result):
    return [(w.source, w.code) for w in result.warnings]


def _read(path, entry):
    with zipfile.ZipFile(path) as archive:
        return archive.read(entry).decode()


def _write(tmp_path, *parts, carries_credential=lambda s: False, token=None):
    target = str(tmp_path / "a.zip")
    result = write_archive(
        target, parts, SINCE, UNTIL, carries_credential, token or CancelToken()
    )
    return target, result


def test_records_are_written_oldest_first_with_a_manifest(tmp_path):
    source = ListSource("server", records((T0 + 2, "second"), (T0 + 1, "first")))

    target, result = _write(tmp_path, ArchivePart(source, 1 << 20, required=True))

    assert _read(target, "server.log") == "first\nsecond\n"
    manifest = json.loads(_read(target, "manifest.json"))
    assert manifest["schema_version"] == 1
    assert {"kalinka_version", "plugin_versions", "os", "architecture"} <= set(manifest)
    assert manifest["requested_range"] == {
        "since": "2026-09-21T14:13:20Z",
        "until": "2026-09-21T15:13:20Z",
    }
    server = manifest["sources"]["server"]
    assert server["entry"] == "server.log"
    assert server["backend"] == "memory"
    assert server["records"] == 2
    assert server["bytes"] == len("first\nsecond\n")
    assert server["first_record"] == "2026-09-21T14:13:21Z"
    assert server["last_record"] == "2026-09-21T14:13:22Z"
    assert server["truncated"] is False
    assert server["time_filter_applied"] is True
    assert result.warnings == []
    assert result.size_bytes == os.stat(target).st_size


def test_the_archive_is_readable_by_its_owner_alone(tmp_path):
    source = ListSource("server", records((T0, "x")))

    target, _ = _write(tmp_path, ArchivePart(source, 1 << 20, required=True))

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    with zipfile.ZipFile(target) as archive:
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in archive.infolist())


def test_the_cap_keeps_the_newest_records_and_says_older_ones_were_dropped(tmp_path):
    source = ListSource(
        "server", records(*((T0 + i, f"record {i:02d}") for i in range(20)))
    )

    target, result = _write(tmp_path, ArchivePart(source, 5 * 10, required=True))

    assert _read(target, "server.log").splitlines() == [
        f"record {i}" for i in range(15, 20)
    ]
    assert _codes(result) == [("server", "older_records_dropped")]
    assert json.loads(_read(target, "manifest.json"))["sources"]["server"]["truncated"]


def test_an_empty_source_gives_an_empty_file_and_a_warning(tmp_path):
    target, result = _write(tmp_path, ArchivePart(ListSource("server"), 1 << 20, required=True))

    assert _read(target, "server.log") == ""
    assert _codes(result) == [("server", "no_records")]


def test_an_unreadable_server_source_fails_the_export_and_leaves_nothing(tmp_path):
    with pytest.raises(ExportFailure) as failure:
        _write(tmp_path, ArchivePart(ListSource("server", fail=True), 1 << 20, required=True))

    assert failure.value.code == "logs_unreadable"
    assert os.listdir(tmp_path) == []


def test_an_unreadable_renderer_source_leaves_a_ready_archive_with_a_warning(tmp_path):
    server = ListSource("server", records((T0, "server line")))
    renderer = ListSource("local_renderer", fail=True)

    target, result = _write(
        tmp_path,
        ArchivePart(server, 1 << 20, required=True),
        ArchivePart(renderer, 1 << 20, required=False),
    )

    assert _read(target, "server.log") == "server line\n"
    assert _read(target, "local-renderer.log") == ""
    assert _codes(result) == [("local_renderer", "source_unreadable")]
    manifest = json.loads(_read(target, "manifest.json"))
    assert manifest["sources"]["local_renderer"]["readable"] is False


def test_history_that_starts_after_the_requested_range_is_a_warning(tmp_path):
    source = ListSource("server", records((T0 + 600, "x")), history_start=T0 + 500)

    _, result = _write(tmp_path, ArchivePart(source, 1 << 20, required=True))

    assert _codes(result) == [("server", "history_shorter")]
    assert "2026-09-21T14:21:40Z" in result.warnings[0].message


def test_history_covering_the_range_is_no_warning(tmp_path):
    source = ListSource("server", records((T0 + 600, "x")), history_start=T0 - 5)

    _, result = _write(tmp_path, ArchivePart(source, 1 << 20, required=True))

    assert result.warnings == []


def test_records_without_time_are_flagged_in_the_manifest_and_warned_about(tmp_path):
    source = ListSource("server", records((None, "<6>x")), time_filtered=False)

    target, result = _write(tmp_path, ArchivePart(source, 1 << 20, required=True))

    assert _codes(result) == [("server", "time_filter_not_applied")]
    manifest = json.loads(_read(target, "manifest.json"))
    assert manifest["sources"]["server"]["time_filter_applied"] is False


def test_a_record_carrying_a_credential_is_left_out_whole(tmp_path, monkeypatch):
    written = []
    real_chunks = log_archive._chunks
    monkeypatch.setattr(
        log_archive, "_chunks", lambda lines: (written.append(c) or c for c in real_chunks(lines))
    )
    source = ListSource(
        "server",
        records(
            (T0, "login with hunter2-secret"),
            (T0 + 1, "GET https://api.example/x?api_key=abc123"),
            (T0 + 2, "mount smb://u:pw@nas/share\nTraceback: still the same record"),
            (T0 + 3, "an ordinary line"),
        ),
    )

    target, result = _write(
        tmp_path,
        ArchivePart(source, 1 << 20, required=True),
        carries_credential=credential_detector(["hunter2-secret"]),
    )

    assert _read(target, "server.log") == "an ordinary line\n"
    for leaked in ("hunter2-secret", "abc123", ":pw@", "Traceback"):
        assert all(leaked.encode() not in chunk for chunk in written)
    assert _codes(result) == [("server", "records_withheld")]
    assert result.warnings[0].message == (
        "Left out 3 server records that seemed to carry a credential."
    )
    manifest = json.loads(_read(target, "manifest.json"))
    assert manifest["sources"]["server"]["records_withheld"] == 3
    assert manifest["sources"]["server"]["records"] == 1


def test_cancelling_mid_collection_stops_and_leaves_nothing(tmp_path):
    gate = threading.Event()
    source = ListSource("server", records((T0, "a"), (T0 + 1, "b")), gate=gate)
    token = CancelToken()
    outcome = {}

    def run():
        try:
            _write(tmp_path, ArchivePart(source, 1 << 20, required=True), token=token)
        except ExportCancelled as e:
            outcome["reason"] = e.reason

    thread = threading.Thread(target=run)
    thread.start()
    assert source.reached_gate.wait(2)
    token.cancel("stop")
    thread.join(2)

    assert outcome == {"reason": "stop"}
    assert os.listdir(tmp_path) == []


def test_a_full_disk_fails_as_insufficient_storage_and_leaves_nothing(tmp_path, monkeypatch):
    def full(path, *args):
        open(path, "wb").close()
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(log_archive, "_write_zip", full)

    with pytest.raises(ExportFailure) as failure:
        _write(tmp_path, ArchivePart(ListSource("server", records((T0, "x"))), 1 << 20, required=True))

    assert failure.value.code == "insufficient_storage"
    assert os.listdir(tmp_path) == []


def test_the_token_closes_a_reading_registered_after_it_was_cancelled():
    token = CancelToken()
    token.cancel("early")
    closed = []

    with token.closing(lambda: closed.append(True)):
        pass

    assert closed == [True]


def test_every_entry_is_dated_when_it_was_written(tmp_path):
    source = ListSource("server", records((T0, "x")))

    target, _ = _write(tmp_path, ArchivePart(source, 1 << 20, required=True))

    with zipfile.ZipFile(target) as archive:
        assert all(info.date_time[0] >= 2026 for info in archive.infolist())
