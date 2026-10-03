"""A module written before SDK 3.8 plays through the default get_track_source."""

import pytest

from kalinka_plugin_sdk.datamodel import Album, EntityId, EntityType, Track
from kalinka_plugin_sdk.inputmodule import (
    DirectUrl,
    InputModule,
    SourceUnavailableError,
    TrackInfo,
    TrackSource,
)


def _info(track_id: str, retriever=None) -> TrackInfo:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="older")

    async def source() -> TrackSource:
        return TrackSource(
            source=DirectUrl(url=f"https://cdn.test/{track_id}.flac"), format="flac"
        )

    return TrackInfo(
        id=entity,
        source_retriever=retriever or source,
        metadata=Track(
            id=entity, title=track_id, duration=1, album=Album(id=entity, title="")
        ),
    )


class _OlderModule(InputModule):
    """Implements get_track_info and nothing of 3.8."""

    def __init__(self, infos: list[TrackInfo]):
        self._infos = infos
        self.asked: list[list[str]] = []

    def module_name(self) -> str:
        return "older"

    async def get_track_info(self, track_ids):
        self.asked.append(list(track_ids))
        return self._infos


async def test_the_default_plays_the_track_get_track_info_returns():
    module = _OlderModule([_info("t1")])

    source = await module.get_track_source("t1")

    assert source.source.url == "https://cdn.test/t1.flac"
    assert module.asked == [["t1"]]


async def test_the_default_picks_the_asked_track_out_of_several():
    module = _OlderModule([_info("other"), _info("t1")])

    source = await module.get_track_source("t1")

    assert source.source.url == "https://cdn.test/t1.flac"


async def test_a_track_the_module_does_not_return_is_not_found():
    module = _OlderModule([_info("other")])

    with pytest.raises(LookupError, match="t1"):
        await module.get_track_source("t1")


async def test_the_retrievers_reason_reaches_the_caller():
    async def unmounted() -> TrackSource:
        raise SourceUnavailableError("Music folder /mnt/nas is not available")

    module = _OlderModule([_info("t1", unmounted)])

    with pytest.raises(SourceUnavailableError, match="/mnt/nas"):
        await module.get_track_source("t1")
