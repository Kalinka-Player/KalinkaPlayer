"""Shared flags for requesting a check from the existing librarian worker."""

import multiprocessing


class ScanControl:
    """One pending check, shared by the server and librarian processes."""

    def __init__(self):
        self.available = multiprocessing.Event()
        self.pending = multiprocessing.Event()
        self.scanning = multiprocessing.Event()

    def request(self) -> None:
        """Coalesce requests until the worker starts the next check."""
        if not self.available.is_set():
            raise RuntimeError("The library indexer is unavailable")
        self.pending.set()
