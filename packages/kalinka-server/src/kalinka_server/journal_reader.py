"""The journal reader: Kalinka's own units and the kernel's records, read out
of the system journal.

The service user cannot read the system journal, and giving it the right to
would hand the whole journal to every plugin the server runs. Instead
``kalinka-journal-reader.socket`` starts this program once per connection,
as a transient user in the ``systemd-journal`` group, with the connection as
its standard input and output.

It reads one request line — ``{"source": ..., "since": ..., "until": ...,
"journal_start": <bool>}``, the times in whole Unix seconds — and answers
with the source's records as ``journalctl --output=json`` lines, newest
first, then one trailer line: ``{"status": "ok", "journal_start": <seconds
or null>}`` or ``{"status": "error", "code": ...}``. Finding the journal's
start takes a second journalctl run, the same for every source, so a client
that already knows it sends ``"journal_start": false`` and the trailer
leaves it out; without the field it is asked for. Journal fields are upper
case, so a trailer's lower-case ``status`` can never be mistaken for a
record.

Only the standard library is imported: this runs with the venv's interpreter
but none of the server.
"""

import grp
import json
import os
import selectors
import subprocess
import sys
from dataclasses import dataclass
from typing import BinaryIO, List, NamedTuple, Optional, Sequence, Tuple


@dataclass(frozen=True)
class JournalQuery:
    """What one source asks the journal for: its ``units``, or ``FIELD=value``
    ``matches`` for records that name no unit. One or the other, since
    journalctl would keep only the records matching both."""

    units: Tuple[str, ...] = ()
    matches: Tuple[str, ...] = ()


#: What each source reads. A unit a package does not ship simply has no
#: records. The kernel's are matched by transport, not ``--dmesg``: that
#: implies ``--boot`` and would lose an OOM report that ended in a reboot.
SOURCE_QUERIES = {
    "server": JournalQuery(
        units=("kalinka.service", "kalinka-upgrade.service", "kalinka-restart.service")
    ),
    "local_renderer": JournalQuery(
        units=("kalinka-renderer.service", "kalinka-renderer-upgrade.service")
    ),
    "supervisor": JournalQuery(
        units=(
            "kalinka-supervisor.service",
            "kalinka-soundcard.service",
            "kalinka-soundcard-apply.service",
            "kalinka-ssh-setup.service",
            "kalinka-wifi@*.service",
            "kalinka-reinstall.service",
            "kalinka-plugin-removal.service",
        )
    ),
    "kernel": JournalQuery(matches=("_TRANSPORT=kernel",)),
}

#: The message and priority, and every field that can say what a record is
#: about: a unit, or the kernel's transport and the identifier of whatever
#: wrote to the kernel's log, since early-boot systemd writes there too.
OUTPUT_FIELDS = (
    "MESSAGE",
    "PRIORITY",
    "_SYSTEMD_UNIT",
    "UNIT",
    "OBJECT_SYSTEMD_UNIT",
    "COREDUMP_UNIT",
    "_TRANSPORT",
    "SYSLOG_IDENTIFIER",
)

JOURNALCTL = "journalctl"
JOURNAL_GROUP = "systemd-journal"

_MAX_REQUEST_BYTES = 1024
_CHUNK = 64 * 1024


class BadRequest(Exception):
    """The request is not one this reader answers."""


class Request(NamedTuple):
    """A validated request: a source, its range, and whether to report the
    journal's start."""

    source: str
    since: int
    until: int
    journal_start: bool


_REQUIRED_FIELDS = {"source", "since", "until"}


def parse_request(line: bytes) -> Request:
    """What a request line asks for.

    @raise BadRequest For anything but exactly a known source, two
        non-negative integer times in order and, optionally, a boolean
        ``journal_start``.
    """
    try:
        request = json.loads(line)
    except ValueError:
        raise BadRequest("not JSON")
    if not isinstance(request, dict) or set(request) - {"journal_start"} != _REQUIRED_FIELDS:
        raise BadRequest("wrong fields")
    source, since, until = request["source"], request["since"], request["until"]
    journal_start = request.get("journal_start", True)
    if source not in SOURCE_QUERIES:
        raise BadRequest("unknown source")
    for value in (since, until):
        if type(value) is not int or value < 0:
            raise BadRequest("times must be whole seconds")
    if since > until:
        raise BadRequest("range ends before it starts")
    if type(journal_start) is not bool:
        raise BadRequest("journal_start must be true or false")
    return Request(source, since, until, journal_start)


def journalctl_argv(source: str, since: int, until: int) -> List[str]:
    """The fixed command for a validated request. No ``--boot``: history
    across reboots is the point."""
    query = SOURCE_QUERIES[source]
    return [
        JOURNALCTL,
        *(f"--unit={unit}" for unit in query.units),
        f"--since=@{since}",
        f"--until=@{until}",
        "--reverse",
        "--output=json",
        f"--output-fields={','.join(OUTPUT_FIELDS)}",
        "--all",
        "--quiet",
        "--no-pager",
        *query.matches,
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
        request = parse_request(_read_request(conn_in))
    except BadRequest as e:
        print(f"Refused a request: {e}", file=sys.stderr)
        _trailer(conn_out, status="error", code="bad_request")
        return 2
    if not can_read_journal():
        print(f"Not in the {JOURNAL_GROUP} group", file=sys.stderr)
        _trailer(conn_out, status="error", code="journal_unreadable")
        return 1
    argv = journalctl_argv(request.source, request.since, request.until)
    status = _stream(argv, conn_in.fileno(), conn_out)
    if status is None:
        return 0
    if status != 0:
        _trailer(conn_out, status="error", code="journal_unreadable")
        return 1
    if request.journal_start:
        _trailer(conn_out, status="ok", journal_start=_journal_start())
    else:
        _trailer(conn_out, status="ok")
    return 0


def main() -> int:
    return serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
