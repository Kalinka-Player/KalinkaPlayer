"""The journal reader: Kalinka's own units, read out of the system journal.

The service user cannot read the system journal, and giving it the right to
would hand the whole journal to every plugin the server runs. Instead
``kalinka-journal-reader.socket`` starts this program once per connection,
as a transient user in the ``systemd-journal`` group, with the connection as
its standard input and output.

It reads one request line — ``{"source": ..., "since": ..., "until": ...}``,
the times in whole Unix seconds — and answers with the source's records as
``journalctl --output=json`` lines, newest first, then one trailer line:
``{"status": "ok", "journal_start": <seconds or null>}`` or
``{"status": "error", "code": ...}``. Journal fields are upper case, so a
trailer's lower-case ``status`` can never be mistaken for a record.

Only the standard library is imported: this runs with the venv's interpreter
but none of the server.
"""

import grp
import json
import os
import selectors
import subprocess
import sys
from typing import BinaryIO, List, Optional, Sequence, Tuple

#: Journal units each source covers. A unit a package does not ship simply
#: has no records.
SOURCE_UNITS = {
    "server": ("kalinka.service", "kalinka-upgrade.service", "kalinka-restart.service"),
    "local_renderer": ("kalinka-renderer.service", "kalinka-renderer-upgrade.service"),
}

#: Every field that can name the unit a record is about, besides the message.
OUTPUT_FIELDS = (
    "MESSAGE",
    "PRIORITY",
    "_SYSTEMD_UNIT",
    "UNIT",
    "OBJECT_SYSTEMD_UNIT",
    "COREDUMP_UNIT",
)

JOURNALCTL = "journalctl"
JOURNAL_GROUP = "systemd-journal"

_MAX_REQUEST_BYTES = 1024
_CHUNK = 64 * 1024


class BadRequest(Exception):
    """The request is not one this reader answers."""


def parse_request(line: bytes) -> Tuple[str, int, int]:
    """The source and time range a request line asks for.

    @raise BadRequest For anything but exactly a known source and two
        non-negative integer times in order.
    """
    try:
        request = json.loads(line)
    except ValueError:
        raise BadRequest("not JSON")
    if not isinstance(request, dict) or set(request) != {"source", "since", "until"}:
        raise BadRequest("wrong fields")
    source, since, until = request["source"], request["since"], request["until"]
    if source not in SOURCE_UNITS:
        raise BadRequest("unknown source")
    for value in (since, until):
        if type(value) is not int or value < 0:
            raise BadRequest("times must be whole seconds")
    if since > until:
        raise BadRequest("range ends before it starts")
    return source, since, until


def journalctl_argv(source: str, since: int, until: int) -> List[str]:
    """The fixed command for a validated request. No ``--boot``: history
    across reboots is the point."""
    return [
        JOURNALCTL,
        *(f"--unit={unit}" for unit in SOURCE_UNITS[source]),
        f"--since=@{since}",
        f"--until=@{until}",
        "--reverse",
        "--output=json",
        f"--output-fields={','.join(OUTPUT_FIELDS)}",
        "--all",
        "--quiet",
        "--no-pager",
    ]


def journal_start_argv() -> List[str]:
    """Reads the oldest record the journal still holds, of any unit; only its
    timestamp is used."""
    return [JOURNALCTL, "--output=json", "--output-fields=PRIORITY", "--quiet", "--no-pager"]


def can_read_journal() -> bool:
    """Whether journalctl will see the system journal rather than silently
    showing only this user's own, empty one."""
    if os.geteuid() == 0:
        return True
    try:
        gid = grp.getgrnam(JOURNAL_GROUP).gr_gid
    except KeyError:
        return False
    return gid in os.getgroups() or gid == os.getegid()


def _read_request(conn_in: BinaryIO) -> bytes:
    line = conn_in.readline(_MAX_REQUEST_BYTES + 1)
    if len(line) > _MAX_REQUEST_BYTES or not line.endswith(b"\n"):
        raise BadRequest("request too long or unterminated")
    return line


def _stream(argv: Sequence[str], conn_in_fd: int, conn_out: BinaryIO) -> Optional[int]:
    """Copy journalctl's output to the connection until it ends.

    @return journalctl's exit status, or None when the peer went away first;
        either way journalctl has been reaped.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "journal")
    # The peer never writes after its request, so readable means it closed.
    selector.register(conn_in_fd, selectors.EVENT_READ, "peer")
    try:
        while True:
            for key, _ in selector.select():
                if key.data == "peer":
                    return None
                chunk = os.read(proc.stdout.fileno(), _CHUNK)
                if not chunk:
                    return proc.wait()
                conn_out.write(chunk)
                conn_out.flush()
    except (BrokenPipeError, ConnectionResetError):
        return None
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def _journal_start() -> Optional[float]:
    proc = subprocess.Popen(
        journal_start_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
    )
    assert proc.stdout is not None
    try:
        first = proc.stdout.readline()
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
    try:
        return int(json.loads(first)["__REALTIME_TIMESTAMP"]) / 1_000_000
    except (ValueError, KeyError, TypeError):
        return None


def _trailer(conn_out: BinaryIO, **fields) -> None:
    try:
        conn_out.write(json.dumps(fields).encode() + b"\n")
        conn_out.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def serve(conn_in: BinaryIO, conn_out: BinaryIO) -> int:
    """Answer one request on an already-accepted connection."""
    try:
        source, since, until = parse_request(_read_request(conn_in))
    except BadRequest as e:
        print(f"Refused a request: {e}", file=sys.stderr)
        _trailer(conn_out, status="error", code="bad_request")
        return 2
    if not can_read_journal():
        print(f"Not in the {JOURNAL_GROUP} group", file=sys.stderr)
        _trailer(conn_out, status="error", code="journal_unreadable")
        return 1
    status = _stream(journalctl_argv(source, since, until), conn_in.fileno(), conn_out)
    if status is None:
        return 0
    if status != 0:
        _trailer(conn_out, status="error", code="journal_unreadable")
        return 1
    _trailer(conn_out, status="ok", journal_start=_journal_start())
    return 0


def main() -> int:
    return serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
