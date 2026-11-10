"""Request and reply between processes over a pair of multiprocessing queues."""

from __future__ import annotations

import logging
import multiprocessing
import queue
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__.split(".")[-1])

_REQUEST_ID = "request_id"


class RequestChannel:
    """@brief The asking side of a request queue and its reply queue.

    Every request carries an id that the answering side echoes through
    reply(), and ask() returns only the reply carrying its own. A reply that
    arrives after its request gave up is dropped by the next ask() rather
    than answering it, so one slow answer cannot shift every later one.

    @note Thread-safe: concurrent asks wait their turn, since one ask would
    drop the reply another is waiting for. The answering side must handle
    requests in order.
    """

    def __init__(
        self,
        requests: multiprocessing.Queue,
        replies: multiprocessing.Queue,
        timeout: float,
    ):
        self._requests = requests
        self._replies = replies
        self._timeout = timeout
        self._lock = threading.Lock()

    def ask(self, request: dict) -> Optional[dict]:
        """Send ``request`` and wait for its reply.

        @return The reply, or None when none came within the timeout, which
        covers the wait for an earlier ask to finish.
        """
        deadline = time.monotonic() + self._timeout
        if not self._lock.acquire(timeout=self._timeout):
            return None
        try:
            return self._exchange(request, deadline)
        finally:
            self._lock.release()

    def _exchange(self, request: dict, deadline: float) -> Optional[dict]:
        if deadline <= time.monotonic():
            return None
        request_id = uuid.uuid4().hex
        self._requests.put({**request, _REQUEST_ID: request_id})
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                answer = self._replies.get(timeout=remaining)
            except queue.Empty:
                break
            if answer.get(_REQUEST_ID) == request_id:
                return answer
            logger.info("Dropped a reply that came after its request gave up")
        return None


def reply(replies: multiprocessing.Queue, request: dict, answer: dict) -> None:
    """Answer ``request`` on ``replies`` under the id it was asked with."""
    replies.put({**answer, _REQUEST_ID: request.get(_REQUEST_ID)})
