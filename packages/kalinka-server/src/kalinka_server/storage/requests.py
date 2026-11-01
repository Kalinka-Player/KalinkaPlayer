"""Mount requests handed to the root helper, and what it made of them."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from kalinka_plugin_sdk import paths


def requests_dir() -> str:
    return os.path.join(paths.prefix(), "run", "kalinka-mounts", "requests")


def results_dir() -> str:
    return os.path.join(paths.prefix(), "run", "kalinka-mounts", "results")

#: A request the helper has not answered by then is reported as failed: on a
#: plain install without the units nothing will ever answer it. Requests run
#: one at a time, so the wait counts from the helper's last answer too, and a
#: share that is away retries for a minute.
ANSWER_TIMEOUT_S = 150.0


@dataclass(frozen=True)
class RequestOutcome:
    """The helper's answer to the last request for one volume.

    @param seq The sequence of the request it answers.
    """

    ok: bool
    error: Optional[str]
    at: float
    seq: str = ""


class MountRequests:
    """Writes requests where ``kalinka-mounts.path`` notices them.

    Each request is a file of its own, ``<sequence>.<volume id>.json``, so
    two asked one after the other, as a remount's unmount and mount are, both
    reach the helper and in that order. The helper's result names the
    sequence it answers.

    @note Thread-safe.
    """

    def __init__(self, requests: Optional[str] = None, results: Optional[str] = None):
        self._requests_dir = requests or requests_dir()
        self._results_dir = results or results_dir()
        self._sent: dict[str, tuple[str, float]] = {}
        self._last = 0
        self._lock = threading.Lock()

    def mount(self, volume_id: str, fstype: str, source: str, options: list[str]) -> None:
        self._write(
            volume_id,
            {"action": "mount", "fstype": fstype, "source": source, "options": options},
        )

    def unmount(self, volume_id: str, eject: bool = False) -> None:
        self._write(volume_id, {"action": "unmount", "eject": eject})

    def _write(self, volume_id: str, body: dict) -> None:
        with self._lock:
            self._last = max(time.time_ns(), self._last + 1)
            seq = f"{self._last:020d}"
            name = f"{seq}.{volume_id}.json"
            partial = os.path.join(self._requests_dir, f".{name}.part")
            with open(partial, "w", encoding="utf-8") as f:
                json.dump({"id": volume_id, "seq": seq, **body}, f)
            os.replace(partial, os.path.join(self._requests_dir, name))
            self._sent[volume_id] = (seq, time.time())

    def pending(self, volume_id: str) -> bool:
        """Whether a request is out and neither answered nor timed out."""
        sent = self._sent.get(volume_id)
        if sent is None:
            return False
        seq, at = sent
        answered = self._answer(volume_id)
        if answered is not None and answered.seq >= seq:
            return False
        return not self._overdue(at)

    def outcome(self, volume_id: str) -> Optional[RequestOutcome]:
        """The helper's last answer for ``volume_id``, or a timeout once the
        request has waited too long; None when nothing was asked."""
        answered = self._answer(volume_id)
        sent = self._sent.get(volume_id)
        if sent is not None and (answered is None or answered.seq < sent[0]):
            if self._overdue(sent[1]):
                return RequestOutcome(False, "The mount helper did not answer", sent[1], sent[0])
            return None
        return answered

    def _overdue(self, sent_at: float) -> bool:
        """Whether a request sent at ``sent_at`` has waited too long: since it
        was sent, and since the helper last answered any, as one queued
        behind a slow mount waits its turn."""
        try:
            answered_at = os.stat(self._results_dir).st_mtime
        except OSError:
            answered_at = 0.0
        return time.time() - max(sent_at, answered_at) >= ANSWER_TIMEOUT_S

    def _answer(self, volume_id: str) -> Optional[RequestOutcome]:
        path = os.path.join(self._results_dir, f"{volume_id}.json")
        try:
            with open(path, encoding="utf-8") as f:
                result = json.load(f)
            return RequestOutcome(
                bool(result.get("ok")),
                result.get("error"),
                float(result.get("at", 0)),
                str(result.get("seq", "")),
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return None
