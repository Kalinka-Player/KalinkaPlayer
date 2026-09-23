"""The journal reader answers one narrow question and nothing else.

It runs with the right to read the whole system journal, so what it accepts
is the security boundary: a known source and a time range, never a unit,
path or command of the caller's choosing. A caller that goes away must not
leave journalctl running.
"""

import json
import os
import socket
import threading
import time

import pytest

from kalinka_server import journal_reader
from kalinka_server.journal_reader import (
    BadRequest,
    journalctl_argv,
    parse_request,
    serve,
)

RECORD = {
    "__REALTIME_TIMESTAMP": "1700000100000000",
    "MESSAGE": "hello",
    "_SYSTEMD_UNIT": "kalinka.service",
    "PRIORITY": "6",
}


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"[]\n",
        b'{"source": "server", "since": 1}\n',
        b'{"source": "server", "since": 1, "until": 2, "unit": "sshd.service"}\n',
        b'{"source": "sshd", "since": 1, "until": 2}\n',
        b'{"source": "server", "since": "1", "until": 2}\n',
        b'{"source": "server", "since": 1.5, "until": 2}\n',
        b'{"source": "server", "since": true, "until": 2}\n',
        b'{"source": "server", "since": -1, "until": 2}\n',
        b'{"source": "server", "since": 3, "until": 2}\n',
    ],
)
def test_anything_but_a_known_source_and_a_range_is_refused(line):
    with pytest.raises(BadRequest):
        parse_request(line)


def test_a_valid_request_is_understood():
    assert parse_request(b'{"source": "local_renderer", "since": 5, "until": 9}\n') == (
        "local_renderer",
        5,
        9,
    )


def test_the_command_names_only_the_source_s_own_units_across_every_boot():
    argv = journalctl_argv("server", 100, 200)

    assert argv[0] == "journalctl"
    assert [a for a in argv if a.startswith("--unit=")] == [
        "--unit=kalinka.service",
        "--unit=kalinka-upgrade.service",
        "--unit=kalinka-restart.service",
    ]
    assert "--since=@100" in argv and "--until=@200" in argv
    assert "--reverse" in argv and "--output=json" in argv
    assert not any(a in ("-b", "--boot") or a.startswith("--boot=") for a in argv)
    assert not any(a.startswith(("--directory", "--file", "--root", "-D")) for a in argv)


@pytest.fixture
def fake_journalctl(tmp_path, monkeypatch):
    """A journalctl on PATH that logs its arguments and prints $RECORDS."""
    binv = tmp_path / "bin"
    binv.mkdir()
    records = tmp_path / "records.jsonl"
    records.write_text("")
    script = binv / "journalctl"
    script.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{tmp_path}/args.log"\n'
        'case " $* " in\n'
        '  *" --reverse "*)\n'
        f'    cat "{records}"\n'
        f'    if [ -f "{tmp_path}/hang" ]; then echo $$ > "{tmp_path}/pid"; exec sleep 30; fi\n'
        f'    [ -f "{tmp_path}/fail" ] && exit 1\n'
        "    exit 0;;\n"
        "  *) echo '{\"__REALTIME_TIMESTAMP\":\"1690000000000000\"}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binv}:{os.environ['PATH']}")
    monkeypatch.setattr(journal_reader, "can_read_journal", lambda: True)
    return tmp_path


def _ask(request: bytes, timeout=10.0):
    """Run the reader on one end of a socket pair; return the other end and
    the thread serving it."""
    ours, theirs = socket.socketpair()
    ours.settimeout(timeout)
    conn_in = theirs.makefile("rb")
    conn_out = theirs.makefile("wb")
    result = {}

    def run():
        result["code"] = serve(conn_in, conn_out)
        conn_in.close()
        conn_out.close()
        theirs.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ours.sendall(request)
    return ours, thread, result


def _answer(ours):
    return [json.loads(line) for line in ours.makefile("rb")]


def test_records_stream_back_then_an_ok_trailer_with_the_journal_start(fake_journalctl):
    (fake_journalctl / "records.jsonl").write_text(json.dumps(RECORD) + "\n")

    ours, thread, _ = _ask(b'{"source": "server", "since": 1, "until": 2}\n')
    lines = _answer(ours)
    thread.join(5)

    assert lines == [RECORD, {"status": "ok", "journal_start": 1690000000.0}]


def test_a_failing_journalctl_is_reported_as_unreadable(fake_journalctl):
    (fake_journalctl / "fail").touch()

    ours, thread, _ = _ask(b'{"source": "server", "since": 1, "until": 2}\n')

    assert _answer(ours) == [{"status": "error", "code": "journal_unreadable"}]


def test_a_reader_without_journal_access_says_so_instead_of_answering_empty(
    fake_journalctl, monkeypatch
):
    monkeypatch.setattr(journal_reader, "can_read_journal", lambda: False)

    ours, thread, _ = _ask(b'{"source": "server", "since": 1, "until": 2}\n')

    assert _answer(ours) == [{"status": "error", "code": "journal_unreadable"}]
    assert not (fake_journalctl / "args.log").exists()


def test_a_bad_request_runs_nothing(fake_journalctl):
    ours, thread, result = _ask(b'{"source": "sshd", "since": 1, "until": 2}\n')

    assert _answer(ours) == [{"status": "error", "code": "bad_request"}]
    thread.join(5)
    assert result["code"] != 0
    assert not (fake_journalctl / "args.log").exists()


def test_an_overlong_request_is_refused(fake_journalctl):
    ours, thread, _ = _ask(b"{" + b" " * 4096 + b"}\n")

    assert _answer(ours) == [{"status": "error", "code": "bad_request"}]


def test_journalctl_is_killed_and_reaped_when_the_caller_hangs_up(fake_journalctl):
    (fake_journalctl / "hang").touch()
    ours, thread, _ = _ask(b'{"source": "server", "since": 1, "until": 2}\n')
    pid_file = fake_journalctl / "pid"
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    pid = int(pid_file.read_text())

    ours.close()
    thread.join(5)

    assert not thread.is_alive()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
