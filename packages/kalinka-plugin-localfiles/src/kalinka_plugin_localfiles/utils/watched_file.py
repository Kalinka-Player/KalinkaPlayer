"""A file whose read failures are remembered.

Tag and image parsers read through the file they are handed and report what
went wrong in their own terms: mutagen re-raises an ``OSError`` as one of its
own errors, and Pillow's "image file is truncated" is an ``OSError`` too.
Whether the bytes could not be *read* or could not be *parsed* is the
difference between a share that did not answer and a file that is broken, so
the reads themselves are watched.
"""

from __future__ import annotations

import errno
import io
from typing import BinaryIO, Callable, Optional, Tuple, TypeVar

_T = TypeVar("_T")


class WatchedFile:
    """Reads, seeks and tells through to a file, keeping the first
    ``OSError`` any of them raised other than a seek before the start.

    Only those three are offered, which is all Pillow and mutagen use on a
    stream they did not open themselves.
    """

    def __init__(self, file: BinaryIO) -> None:
        self._file = file
        #: What reading the file raised, or None while every read worked.
        self.read_error: Optional[OSError] = None

    def read(self, size: int = -1) -> bytes:
        return self._watch(self._file.read, size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        # EINVAL is a seek before the start, which mutagen makes on any file
        # too short for the tag it looks for, and recovers from.
        return self._watch(self._file.seek, offset, whence, harmless=(errno.EINVAL,))

    def tell(self) -> int:
        return self._watch(self._file.tell)

    def _watch(
        self, call: Callable[..., _T], *args, harmless: Tuple[int, ...] = ()
    ) -> _T:
        try:
            return call(*args)
        except OSError as e:
            if self.read_error is None and e.errno not in harmless:
                self.read_error = e
            raise
