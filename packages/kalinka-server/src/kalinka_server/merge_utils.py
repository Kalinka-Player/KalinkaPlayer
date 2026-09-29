import asyncio
import heapq
from typing import Awaitable, Callable, Sequence

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    EmptyList,
    FavoriteIds,
)
from kalinka_plugin_sdk.inputmodule import InputModule

BrowseItemsSource = Callable[[int, int], Awaitable[BrowseItemList | None]]
ComparedValue = Callable[[BrowseItem], int]


async def _fetch(source: BrowseItemsSource, offset: int, limit: int) -> BrowseItemList:
    """A source's page, or an empty one when it answers None: a module that
    leaves an SDK call at its default body has nothing to list."""
    page = await source(offset, limit)
    return EmptyList(offset, limit) if page is None else page


async def k_way_merge_browse_items(
    data_sources: list[BrowseItemsSource],
    compared_value: ComparedValue,
    offset: int = 0,
    limit: int = 25,
) -> BrowseItemList:
    """
    Performs K-way merge of BrowseItemList objects based on timestamp field.
    The items in each data source should be sorted by timestamp in descending order.

    Args:
        data_sources: List of functions that return BrowseItemList objects.
        offset: Number of items to skip from the beginning
        limit: Maximum number of items to return

    Returns:
        Merged list of items sorted by timestamp in descending order
    """

    # Min heap to store (negative_timestamp, list_index, item_index, item)
    heap = []

    # Track state for each data source
    source_states = []

    # Calculate smart initial chunk size based on total work needed
    total_needed = offset + limit
    initial_chunk_size = max(50, total_needed // max(1, len(data_sources)) * 2)
    initial_chunk_size = min(initial_chunk_size, 200)  # Cap to avoid excessive memory

    # Initialize each data source in parallel
    async def fetch_initial_data(list_idx, data_source):
        browse_list = await _fetch(data_source, 0, initial_chunk_size)
        return list_idx, browse_list

    # Fetch initial data from all sources in parallel
    initial_fetch_tasks = [
        fetch_initial_data(idx, source) for idx, source in enumerate(data_sources)
    ]
    initial_results = await asyncio.gather(*initial_fetch_tasks)

    total_results = 0

    # Process initial results and set up source states
    for list_idx, browse_list in initial_results:
        total_results += browse_list.total
        source_state = {
            "data_source": data_sources[list_idx],
            "current_items": browse_list.items,
            "current_offset": 0,
            "fetched_count": len(browse_list.items),
            "total_available": browse_list.total,
            "exhausted": browse_list.total <= len(browse_list.items),
            "fetch_count": 1,  # Track number of API calls made
        }
        source_states.append(source_state)

        # Add first item to heap if available
        if browse_list.items:
            item = browse_list.items[0]
            index = compared_value(item)
            # Use negative timestamp for max heap behavior (descending order)
            heapq.heappush(heap, (-index, list_idx, 0, item))

    result = []
    processed = 0

    while heap and len(result) < limit:
        neg_index, list_idx, item_idx, item = heapq.heappop(heap)

        # Skip items until we reach the offset
        if processed >= offset:
            result.append(item)

        processed += 1

        # Add next item from the same list if available
        next_item_idx = item_idx + 1
        source_state = source_states[list_idx]

        # Check if we need to fetch more data from this source
        if (
            next_item_idx >= len(source_state["current_items"])
            and not source_state["exhausted"]
        ):
            # Calculate adaptive chunk size for subsequent fetches
            remaining_needed = max(1, (offset + limit) - processed)
            remaining_available = (
                source_state["total_available"] - source_state["fetched_count"]
            )

            # Use larger chunks for subsequent fetches to reduce API calls
            adaptive_chunk_size = min(
                max(
                    100, remaining_needed * 2
                ),  # At least 100, preferably 2x what we need
                remaining_available,  # Don't fetch more than available
                300,  # Cap at 300 to avoid excessive memory
            )

            # Fetch next chunk asynchronously using the async data source
            new_offset = source_state["fetched_count"]
            browse_list = await _fetch(
                source_state["data_source"],
                new_offset,
                adaptive_chunk_size,
            )
            source_state["fetch_count"] += 1

            # Extend current items with new data
            source_state["current_items"].extend(browse_list.items)
            source_state["fetched_count"] += len(browse_list.items)

            # Check if source is now exhausted
            if (
                source_state["fetched_count"] >= source_state["total_available"]
                or len(browse_list.items) == 0
            ):
                source_state["exhausted"] = True

        # Add next item to heap if available
        if next_item_idx < len(source_state["current_items"]):
            next_item = source_state["current_items"][next_item_idx]
            next_index = compared_value(next_item)
            heapq.heappush(heap, (-next_index, list_idx, next_item_idx, next_item))

    return BrowseItemList(
        offset=offset,
        limit=limit,
        total=total_results,
        items=result,
    )


async def flat_merge(
    data_sources: list[BrowseItemsSource], offset, limit
) -> BrowseItemList:
    """
    Merges multiple BrowseItemList objects into a single list.
    This is a simple merge without any specific sorting or filtering.

    Returns:
        A single BrowseItemList containing all items from the input lists.
    """
    all_items = []
    total_count = 0

    # Fetch data from all sources in parallel
    fetch_tasks = [_fetch(data_source, offset, limit) for data_source in data_sources]
    browse_lists = await asyncio.gather(*fetch_tasks)

    # Merge all results
    for browse_list in browse_lists:
        all_items.extend(browse_list.items)
        total_count += browse_list.total

    # Create a new BrowseItemList with the merged items
    return BrowseItemList(
        offset=offset,
        limit=limit,
        total=total_count,
        items=all_items,
    )


async def get_favorite_ids_merged(modules: Sequence[InputModule]) -> FavoriteIds:
    """
    Get a list of favorite IDs from multiple input modules in parallel.

    This function retrieves all favorite IDs for a specific type across all modules
    and combines them into a single list.

    Args:
        modules: Sequence of InputModule instances to get favorites from

    Returns:
        List of favorite IDs as strings
    """
    ids = FavoriteIds()

    results = await asyncio.gather(*(module.get_favorite_ids() for module in modules))
    for result in results:
        if result:
            ids.albums.extend(result.albums)
            ids.tracks.extend(result.tracks)
            ids.playlists.extend(result.playlists)
            ids.artists.extend(result.artists)
    return ids
