"""A play queue written before SDK 3.10 replaces through the default: a clear,
then an add of the new tracks at the end."""

from kalinka_plugin_sdk.api import PlayQueueController
from kalinka_plugin_sdk.datamodel import Album, EntityId, EntityType, Track


def _track(track_id: str) -> Track:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="older")
    return Track(
        id=entity, title=track_id, duration=1, album=Album(id=entity, title="")
    )


class _OlderQueue(PlayQueueController):
    """Implements clear and add and nothing of 3.10."""

    def __init__(self, tracks: list[Track]):
        self.tracks = tracks
        self.calls: list[str] = []

    async def clear(self):
        self.calls.append("clear")
        self.tracks = []

    async def add(self, tracks, index=None):
        self.calls.append(f"add at {index}")
        self.tracks.extend(tracks)


async def test_the_default_clears_then_adds_the_new_tracks():
    queue = _OlderQueue([_track("old")])
    new = [_track("t1"), _track("t2")]

    await queue.replace(new)

    assert queue.calls == ["clear", "add at None"]
    assert queue.tracks == new
