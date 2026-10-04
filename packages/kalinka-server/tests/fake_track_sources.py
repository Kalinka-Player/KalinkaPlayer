"""The play queue's source resolver, faked for tests that drive a queue."""

from collections.abc import Awaitable, Callable
from typing import Optional

from kalinka_plugin_sdk.datamodel import Album, EntityId, EntityType, Track
from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackInfo, TrackSource
from kalinka_server.track_sources import TrackSources

Retriever = Callable[[], Awaitable[TrackSource]]


def example_track(track_id: str = "1") -> Track:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="test_source")
    return Track(
        id=entity,
        title=f"track{track_id}",
        duration=10,
        album=Album(id=entity, title="album"),
    )


async def example_source(track_id: EntityId) -> TrackSource:
    return TrackSource(
        source=DirectUrl(url=f"http://example/{track_id.id}.flac"), format="FLAC"
    )


class FakeTrackSources(TrackSources):
    """Serves a track from the retriever registered for its id, or else from
    ``default``, which is given the id."""

    def __init__(
        self,
        default: Callable[[EntityId], Awaitable[TrackSource]] = example_source,
    ):
        self._default = default
        self._retrievers: dict[str, Retriever] = {}
        self.resolved: list[str] = []

    def serve(self, track_id: EntityId, retriever: Retriever) -> None:
        self._retrievers[track_id.to_string] = retriever

    def serving(self, *infos: TrackInfo) -> list[TrackInfo]:
        """Registers each TrackInfo's retriever for its id, for a test that
        queues TrackInfos the way plugins used to hand them over."""
        for info in infos:
            self.serve(info.id, info.source_retriever)
        return list(infos)

    async def resolve(self, track_id: EntityId) -> TrackSource:
        self.resolved.append(track_id.to_string)
        retriever = self._retrievers.get(track_id.to_string)
        if retriever is not None:
            return await retriever()
        return await self._default(track_id)

    def unavailable_reason(self, track_id: EntityId) -> Optional[str]:
        return None
