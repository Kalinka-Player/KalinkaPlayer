"""Turning what a client asked to play into tracks the queue can hold.

A container is expanded by its source — by browsing it, unless the source
says adding it takes more than its listing shows, as a folder does — and every
track that comes back is looked up through the source that owns *it*, not
through the container's. The
two are the same source for an album, and need not be for a collection, whose
rows come from wherever they were collected. Looking up by the container would
ask one source for another's ids.

Only metadata is looked up here; the queue asks a track's module for its
source when the track plays.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence

from kalinka_plugin_sdk.datamodel import EntityId, EntityType, Track
from kalinka_plugin_sdk.inputmodule import InputModule

from .browse_source import BrowseSource

logger = logging.getLogger(__name__.split(".")[-1])

# A container's tracks are taken in one page; beyond this a client pages itself.
CONTAINER_LIMIT = 5000


async def tracks_for(
    ids: Sequence[str],
    browse_source_for: Callable[[EntityId], BrowseSource],
    module_for: Callable[[str], InputModule],
) -> List[Track]:
    """The tracks behind ``ids``, in the order they were asked for.

    Ids may name tracks or containers, and a container may hold tracks from
    several sources. Each owning source is asked once, through ``get_all``,
    for the distinct ids it owns; a track it does not return is left out
    rather than faked.

    Raises:
        Whatever a source's browse or lookup raises — a partial queue is worse
        than a failed add, since the gap is silent.
    """
    wanted: List[EntityId] = []
    for raw in ids:
        entity_id = EntityId.from_string(raw)
        if entity_id.type == EntityType.TRACK:
            wanted.append(entity_id)
            continue
        wanted.extend(
            track
            for track in await _contents(browse_source_for(entity_id), entity_id)
            if track.type == EntityType.TRACK
        )

    by_source: Dict[str, Dict[str, EntityId]] = defaultdict(dict)
    for entity_id in wanted:
        by_source[entity_id.source][entity_id.to_string] = entity_id

    resolved: Dict[str, Track] = {}
    for source, distinct in by_source.items():
        for item in await module_for(source).get_all(list(distinct.values())):
            if item.track is not None:
                resolved[item.id.to_string] = item.track

    tracks = [
        resolved[entity_id.to_string]
        for entity_id in wanted
        if entity_id.to_string in resolved
    ]
    if len(tracks) != len(wanted):
        logger.warning(
            "Queue add: %d of %d tracks could not be resolved",
            len(wanted) - len(tracks),
            len(wanted),
        )
    return tracks


async def _contents(source: BrowseSource, container: EntityId) -> List[EntityId]:
    """What adding ``container`` takes, as its source says or else as it lists."""
    listed: Optional[List[EntityId]] = await source.tracks_to_add(
        container, CONTAINER_LIMIT
    )
    if listed is not None:
        return listed[:CONTAINER_LIMIT]
    listing = await source.browse(container, offset=0, limit=CONTAINER_LIMIT)
    return [item.id for item in listing.items]
