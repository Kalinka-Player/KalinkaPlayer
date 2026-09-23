"""Reading a source: the journal through its reader, or the developer's file.

A source tells an empty range apart from one it could not read, keeps its
newest records first, and reports when its history starts later than asked.
"""

import json
import os
import socket
import threading
import time

import pytest

from kalinka_server.log_sources import (
    MAX_RECORD_BYTES,
    FileCatalog,
    FileLogSource,
    JournalCatalog,
    JournalLogSource,
    LogSourceUnreadable,
    cap_record,
    render_journal_record,
)

T0 = 1_790_000_000


def _entry(seconds, message, unit="kalinka.service", **extra):
    return {
        "__REALTIME_TIMESTAMP": str(int(seconds * 1_000_000)),
        "MESSAGE": message,
        "_SYSTEMD_UNIT": unit,
        "PRIORITY": "6",
        **extra,
    }


def test_a_journal_record_reads_as_one_timestamped_line_naming_its_unit():
    record = render_journal_record(_entry(T0 + 0.25, "server: Starting"))

    assert record.timestamp == T0 + 0.25
    assert record.text == "2026-09-21T14:13:20.250Z kalinka.service [info] server: Starting"


def test_systemd_s_own_message_about_a_unit_names_that_unit():
    entry = _entry(T0, "Main process exited, status=1/FAILURE", unit="init.scope")
    entry["UNIT"] = "kalinka.service"
    entry["PRIORITY"] = "4"

    assert " kalinka.service [warning] Main process" in render_journal_record(entry).text


def test_a_message_that_is_not_utf8_is_still_rendered():
    entry = _entry(T0, None)
    entry["MESSAGE"] = list(b"caf\xe9")

    assert render_journal_record(entry).text.endswith("caf�")


def test_a_field_set_several_times_shows_every_value():
    entry = _entry(T0, None)
    entry["MESSAGE"] = ["first", "second"]

    assert render_journal_record(entry).text.endswith("[info] first second")


def test_a_huge_record_is_cut_and_marked():
    text = cap_record("x" * (MAX_RECORD_BYTES + 10))

    assert len(text.encode()) < MAX_RECORD_BYTES + 100
    assert text.endswith(f"[cut: the record was {MAX_RECORD_BYTES + 10} bytes]")


class FakeReader:
    """A journal reader on a Unix socket, answering from a script."""

    def __init__(self, path, lines, *, hang=False):
        self.path = str(path)
        self.lines = lines
        self.hang = hang
        self.requests = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.server.accept()
        with conn:
            self.requests.append(json.loads(conn.makefile("rb").readline()))
            for line in self.lines:
                conn.sendall(line if isinstance(line, bytes) else json.dumps(line).encode() + b"\n")
            if self.hang:
                conn.recv(1)

    def close(self):
        self.server.close()


@pytest.fixture
def reader_at(tmp_path):
    made = []

    def make(lines, **kwargs):
        made.append(FakeReader(tmp_path / "reader.sock", lines, **kwargs))
        return made[-1]

    yield make
    for reader in made:
        reader.close()


def test_the_journal_source_asks_for_its_name_and_range_and_yields_newest_first(reader_at):
    reader = reader_at(
        [_entry(T0 + 2, "newer"), _entry(T0 + 1, "older"), {"status": "ok", "journal_start": T0 - 100}]
    )
    reading = JournalLogSource("server", reader.path).open(T0, T0 + 10)

    texts = [r.text for r in reading.records()]

    assert reader.requests == [{"source": "server", "since": T0, "until": T0 + 10}]
    assert [t.rsplit(" ", 1)[-1] for t in texts] == ["newer", "older"]
    assert reading.history_start == T0 - 100


def test_an_empty_range_is_empty_not_unreadable(reader_at):
    reader = reader_at([{"status": "ok", "journal_start": T0 - 100}])

    assert list(JournalLogSource("server", reader.path).open(T0, T0 + 10).records()) == []


def test_a_reader_that_reports_an_error_makes_the_source_unreadable(reader_at):
    reader = reader_at([{"status": "error", "code": "journal_unreadable"}])

    with pytest.raises(LogSourceUnreadable):
        list(JournalLogSource("server", reader.path).open(T0, T0 + 10).records())


def test_a_reader_that_stops_without_a_trailer_makes_the_source_unreadable(reader_at):
    reader = reader_at([_entry(T0, "partial")])

    with pytest.raises(LogSourceUnreadable):
        list(JournalLogSource("server", reader.path).open(T0, T0 + 10).records())


def test_no_reader_listening_makes_the_source_unreadable(tmp_path):
    with pytest.raises(LogSourceUnreadable):
        JournalLogSource("server", str(tmp_path / "absent.sock")).open(T0, T0 + 10)


def test_closing_a_reading_unblocks_a_pending_read(reader_at):
    reader = reader_at([], hang=True)
    reading = JournalLogSource("server", reader.path).open(T0, T0 + 10)
    failed = threading.Event()

    def consume():
        try:
            list(reading.records())
        except LogSourceUnreadable:
            failed.set()

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    time.sleep(0.1)
    reading.close()
    thread.join(2)

    assert failed.is_set()


def test_an_oversized_journal_line_becomes_a_marked_record(reader_at, monkeypatch):
    monkeypatch.setattr("kalinka_server.log_sources._MAX_READER_LINE", 200)
    big = json.dumps(_entry(T0 + 1, "y" * 1000)).encode() + b"\n"
    reader = reader_at([big, _entry(T0, "small"), {"status": "ok", "journal_start": None}])

    records = list(JournalLogSource("server", reader.path).open(T0, T0 + 10).records())

    assert records[0].timestamp == T0 + 1
    assert "too large" in records[0].text
    assert records[1].text.endswith("small")


def _stamp(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds)) + ".000"


def _write_log(path, lines):
    path.write_text("".join(line + "\n" for line in lines))


def test_the_file_source_keeps_a_traceback_with_its_record_and_filters_by_time(tmp_path):
    log = tmp_path / "server.log"
    _write_log(
        log,
        [
            f"{_stamp(T0 - 100)} INFO 1 server: too old",
            f"{_stamp(T0 + 1)} ERROR 1 server: boom",
            "Traceback (most recent call last):",
            '  File "x.py", line 1',
            f"{_stamp(T0 + 2)} INFO 1 server: after",
            f"{_stamp(T0 + 50)} INFO 1 server: too new",
        ],
    )

    records = list(FileLogSource("server", str(log)).open(T0, T0 + 10).records())

    assert [r.text.split("\n")[0].split("server: ")[1] for r in records] == ["after", "boom"]
    assert records[1].text.endswith('  File "x.py", line 1')
    assert records[1].timestamp == T0 + 1


def test_the_file_source_reports_history_that_starts_late(tmp_path):
    log = tmp_path / "server.log"
    _write_log(log, [f"{_stamp(T0 + 5)} INFO 1 server: first ever"])

    reading = FileLogSource("server", str(log)).open(T0, T0 + 10)
    list(reading.records())

    assert reading.history_start == T0 + 5


def test_journal_format_records_are_kept_unfiltered_and_said_to_be(tmp_path):
    log = tmp_path / "server.log"
    _write_log(log, ["<6>server: one", "<3>server: two"])

    reading = FileLogSource("server", str(log)).open(T0, T0 + 10)
    records = list(reading.records())

    assert [r.text for r in records] == ["<3>server: two", "<6>server: one"]
    assert reading.time_filtered is False


def test_the_file_source_reads_only_what_was_there_when_opened(tmp_path):
    log = tmp_path / "server.log"
    _write_log(log, [f"{_stamp(T0 + 1)} INFO 1 server: before"])
    reading = FileLogSource("server", str(log)).open(T0, T0 + 10)
    with open(log, "a") as f:
        f.write(f"{_stamp(T0 + 2)} INFO 1 server: after open\n")

    assert [r.text.rsplit(": ", 1)[1] for r in reading.records()] == ["before"]


def test_the_file_source_does_not_follow_a_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("secret stuff\n")
    os.symlink(target, tmp_path / "server.log")

    with pytest.raises(LogSourceUnreadable):
        FileLogSource("server", str(tmp_path / "server.log")).open(T0, T0 + 10)


def test_a_long_file_is_read_across_chunk_boundaries(tmp_path):
    log = tmp_path / "server.log"
    lines = [f"{_stamp(T0 + 1)} INFO 1 server: line {i} " + "z" * 100 for i in range(3000)]
    _write_log(log, lines)

    records = list(FileLogSource("server", str(log)).open(T0, T0 + 10).records())

    assert [r.text for r in records] == list(reversed(lines))


def test_the_journal_catalog_offers_the_renderer_only_when_it_is_installed(tmp_path):
    sock = tmp_path / "reader.sock"
    sock.touch()
    unit = tmp_path / "kalinka-renderer.service"

    assert list(JournalCatalog(str(sock), str(unit)).available()) == ["server"]
    unit.touch()
    assert list(JournalCatalog(str(sock), str(unit)).available()) == ["server", "local_renderer"]
    sock.unlink()
    assert JournalCatalog(str(sock), str(unit)).available() == {}


def test_the_file_catalog_never_offers_a_renderer(tmp_path):
    log = tmp_path / "server.log"
    assert FileCatalog(str(log)).available() == {}
    log.touch()
    assert list(FileCatalog(str(log)).available()) == ["server"]
