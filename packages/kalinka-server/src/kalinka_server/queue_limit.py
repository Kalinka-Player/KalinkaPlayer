"""How many tracks the play queue may hold.

A folder can come to thousands of tracks, and a queue that long is slow to
send, to show and to keep. An add that would take the queue past its limit is
refused whole rather than cut short, so what plays is never quietly less than
what was asked for.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from kalinka_plugin_sdk.api import PlayQueueController


@dataclass(frozen=True)
class QueueLimit:
    """The most tracks the queue takes, and the 409 detail refusing more."""

    tracks: int
    refusal: dict

    async def refuse_past(self, playqueue: PlayQueueController, adding: int) -> None:
        """Refuse an add that would take the queue past the limit.

        @param adding Ids or tracks: checked on the ids first, since every id
            costs its source a lookup, and again on the tracks they expanded to.
        """
        queued = (await playqueue.list(offset=0, limit=0)).total
        if queued + adding > self.tracks:
            raise HTTPException(status_code=409, detail=self.refusal)


QUEUE_LIMIT = 1000
QUEUE_FULL = {
    "code": "queue_full",
    "message": (
        f"The queue holds up to {QUEUE_LIMIT} tracks. Remove some, or clear "
        "the queue, to add more."
    ),
}
QUEUE = QueueLimit(QUEUE_LIMIT, QUEUE_FULL)
