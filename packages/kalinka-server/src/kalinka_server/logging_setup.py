"""Journald-aware log formatting shared by the server's entry points."""

import logging
import os
import sys
from typing import IO, Optional


LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(thread)d %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Libraries that log request URLs, headers or authentication exchanges, where
#: API keys, tokens and NTLM responses ride.
CREDENTIAL_CARRYING_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3", "spnego")


def quiet_credential_carrying_loggers() -> None:
    """Hold the libraries that would log a credential at WARNING, whatever the
    server's own level, ``--debug`` included.

    @note Set in the main process, this covers the plugin subprocesses too:
        their records are re-emitted only where the main process's logger of
        the same name is enabled for them.
    """
    for name in CREDENTIAL_CARRYING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _syslog_priority(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return 2
    if levelno >= logging.ERROR:
        return 3
    if levelno >= logging.WARNING:
        return 4
    if levelno >= logging.INFO:
        return 6
    return 7


def stream_is_journal(stream: IO) -> bool:
    """Whether this stream is the one systemd connected to the journal.

    JOURNAL_STREAM is inherited by every descendant of a systemd unit — a shell
    in a systemd-managed desktop session has it set too — so its presence alone
    proves nothing. systemd's contract is that it carries the device:inode of
    the journal stream, to be compared against the stream we actually write to.
    """
    spec = os.environ.get("JOURNAL_STREAM")
    if not spec:
        return False
    try:
        dev, _, ino = spec.partition(":")
        st = os.fstat(stream.fileno())
        return st.st_dev == int(dev) and st.st_ino == int(ino)
    except (AttributeError, OSError, ValueError):
        return False


class JournalFormatter(logging.Formatter):
    """Formats for the systemd journal: an sd-daemon "<N>" priority prefix and
    no timestamp or level text of our own — journald records both itself."""

    def __init__(self) -> None:
        super().__init__("%(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{_syslog_priority(record.levelno)}>"
        # journald reads line by line, so a traceback's lines each need one; a
        # trailing newline would leave a prefix alone on an empty line.
        body = super().format(record).rstrip("\n")
        return prefix + body.replace("\n", "\n" + prefix)


def make_formatter(stream: Optional[IO] = None) -> logging.Formatter:
    """The formatter for `stream` (default stderr, as logging.StreamHandler).

    KALINKA_LOG_FORMAT settles it outright when set to "journal" or "full";
    anything else, including unset, follows the stream. The override exists for
    runs whose output systemd never sees, such as `make dev-run` piping through
    tee.
    """
    mode = os.environ.get("KALINKA_LOG_FORMAT", "").strip().lower()
    if mode not in ("journal", "full"):
        journal = stream_is_journal(stream if stream is not None else sys.stderr)
        mode = "journal" if journal else "full"
    if mode == "journal":
        return JournalFormatter()
    return logging.Formatter(LOG_FORMAT, DATE_FORMAT)


def make_handler(stream: Optional[IO] = None) -> logging.StreamHandler:
    """A stream handler carrying the formatter its own stream calls for.

    Pairing the two here is what keeps the choice honest: a formatter picked
    for one stream and attached to another silently logs in the wrong format.
    """
    handler = logging.StreamHandler(stream) if stream else logging.StreamHandler()
    handler.setFormatter(make_formatter(handler.stream))
    return handler
