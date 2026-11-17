"""Local tracks cleared for serving lately, so a renderer's next range of one
skips the checks that cleared it."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

#: How long a full clearance stands, however busy the track: as long as a
#: share's measured length (see ``storage.reader_pool.REMEASURE_S``).
WINDOW_S = 120.0

#: Tracks remembered at once: the one playing and the one queued after it,
#: for a couple of renderers.
CAPACITY = 8


@dataclass(frozen=True)
class VouchedTrack:
    """A local file the full checks cleared.

    ``identity`` is the file's device and inode then. A file at the same path
    with another identity is not the one cleared: replaced in place, or a stray
    file in the directory a lost mount left behind.
    """

    track_path: str
    local_path: str
    mime_type: str
    identity: tuple[int, int]


class VouchedTracks:
    """What ``get_content_info`` cleared in full in the last ``window_s``, by
    asset id.

    A renderer asks for a track a range at a time, several times a second for
    DSD, and each range repeated the track lookup, the folder probe and the
    mount identity check, which cost two database connections and a probe
    apiece. Within the window, the caller serves the same file again once it
    has checked the boundary and the identity. Anything else goes back to the
    full checks.

    @note Event loop only: not thread-safe.
    """

    def __init__(
        self,
        window_s: float = WINDOW_S,
        capacity: int = CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._capacity = capacity
        self._clock = clock
        self._tracks: OrderedDict[str, tuple[float, VouchedTrack]] = OrderedDict()

    def get(self, asset_id: str) -> Optional[VouchedTrack]:
        """The clearance of ``asset_id`` while it stands, else None."""
        entry = self._tracks.get(asset_id)
        if entry is None:
            return None
        cleared_at, track = entry
        if self._clock() - cleared_at >= self._window_s:
            del self._tracks[asset_id]
            return None
        return track

    def put(self, asset_id: str, track: VouchedTrack) -> None:
        """Record a full clearance of ``asset_id``, from now."""
        self._tracks.pop(asset_id, None)
        self._tracks[asset_id] = (self._clock(), track)
        while len(self._tracks) > self._capacity:
            self._tracks.popitem(last=False)

    def forget(self, asset_id: str) -> None:
        self._tracks.pop(asset_id, None)
