"""A storage that refuses, so a misconfigured folder is reported and not fatal.

A music folder may name a protocol this module does not speak, or one whose
client library is not installed. Neither is a reason for a scan to raise:
the folder is simply unreachable, which is a state the library already knows
how to hold — rows under an unavailable root are kept, not purged, and the
reason is shown on the module's settings page.
"""

from __future__ import annotations

from typing import BinaryIO, Iterable, Optional

from .base import DirEntry, FileStat, FileStorage, RootStatus
from .locator import is_within, scheme_of


class UnavailableStorage(FileStorage):
    """Answers every read with the reason it cannot be served.

    @param scheme The protocol it stands in for, so the resolver matches it
        the way it matches a working one.
    @param reason What to tell the user, phrased for the settings page.
    """

    def __init__(self, scheme: str, reason: str) -> None:
        super().__init__()
        self._scheme = scheme
        self._reason = reason

    @property
    def scheme(self) -> str:
        return self._scheme

    def handles(self, path: str) -> bool:
        return scheme_of(path) == self._scheme

    def canonical(self, path: str) -> str:
        """The location unchanged: it cannot be canonicalised without the
        protocol, and keeping it is what lets it appear as a broken root
        rather than silently vanish from the configuration."""
        return (path or "").strip()

    def contains(self, path: str, roots: Iterable[str]) -> bool:
        return any(is_within(path, root) for root in roots)

    def listdir(self, path: str) -> list[DirEntry]:
        raise OSError(self._reason)

    def stat(self, path: str) -> FileStat:
        raise OSError(self._reason)

    def open(self, path: str) -> BinaryIO:
        raise OSError(self._reason)

    def local_path(self, path: str) -> Optional[str]:
        return None

    def probe_root_blocking(self, root: str) -> RootStatus:
        return self.unavailable(root, self._reason)
