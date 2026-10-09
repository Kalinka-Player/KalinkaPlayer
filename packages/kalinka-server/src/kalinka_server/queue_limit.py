"""How many tracks the play queue may hold.

A folder can come to thousands of tracks, and a queue that long is slow to
send, to show and to keep. An add that would take the queue past its limit is
refused whole rather than cut short, so what plays is never quietly less than
what was asked for. A replacement of the queue is refused the same way, and
leaves the queue as it was.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from kalinka_plugin_sdk.api import PlayQueueController


@dataclass(frozen=True)
class QueueLimit:
    """The most tracks the queue takes, and the 409 details refusing more."""

    tracks: int
    refusal: dict
    #: The add's code, without its advice to clear the queue, which cannot
    #: make room for a replacement.
    replacement_refusal: dict

    async def refuse_past(self, playqueue: PlayQueueController, adding: int) -> None:
        """Refuse an add that would take the queue past the limit.

        @param adding Ids or tracks: checked on the ids first, since every id
            costs its source a lookup, and again on the tracks they expanded to.
        """
        queued = (await playqueue.list(offset=0, limit=0)).total
        if queued + adding > self.tracks:
            raise HTTPException(status_code=409, detail=self.refusal)

    def refuse_replacement(self, tracks: int) -> None:
        """Refuse a replacement of the queue by more tracks than it holds.

        A replacement is checked on its own tracks alone, since the ones queued
        now make way for it.
        """
        if tracks > self.tracks:
            raise HTTPException(status_code=409, detail=self.replacement_refusal)


QUEUE_LIMIT = 1000
QUEUE_FULL = {
    "code": "queue_full",
    "message": (
        f"The queue holds up to {QUEUE_LIMIT} tracks. Remove some, or clear "
        "the queue, to add more."
    ),
}
TOO_MANY_TO_PLAY = {
    "code": QUEUE_FULL["code"],
    "message": f"The queue holds up to {QUEUE_LIMIT} tracks. Choose fewer to play.",
}
QUEUE = QueueLimit(QUEUE_LIMIT, QUEUE_FULL, TOO_MANY_TO_PLAY)
