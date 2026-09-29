"""
Test module for k_way_merge_browse_items function in merge_utils.py
"""

import pytest
import asyncio
import time
from typing import List

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    EntityId,
    EntityType,
)
from kalinka_server.merge_utils import k_way_merge_browse_items, flat_merge


def create_browse_item(name: str, timestamp: int, source: str = "test") -> BrowseItem:
    """Helper to create a BrowseItem for testing"""
    entity_id = EntityId(
        id=name.lower().replace(" ", "_"), type=EntityType.TRACK, source=source
    )
    return BrowseItem(
        id=entity_id,
        name=name,
        url="",
        can_browse=True,
        can_add=True,
        timestamp=timestamp,
    )


def compared_value(item: BrowseItem) -> int:
    """Helper to get the timestamp or 0 if None"""
    return item.timestamp if item.timestamp is not None else 0


class MockDataSource:
    """Mock data source that simulates API calls with timing"""

    def __init__(
        self, items_data: List[tuple], source_name: str, call_delay: float = 0.1
    ):
        self.all_items = [
            create_browse_item(name, ts, source_name) for name, ts in items_data
        ]
        self.source_name = source_name
        self.call_delay = call_delay
        self.call_count = 0
        self.call_history = []

    async def __call__(self, offset: int, limit: int) -> BrowseItemList:
        """Simulate async data source call with delay"""
        self.call_count += 1
        start_time = time.time()

        # Simulate network delay asynchronously
        await asyncio.sleep(self.call_delay)

        start_idx = offset
        end_idx = min(offset + limit, len(self.all_items))
        items = self.all_items[start_idx:end_idx]

        result = BrowseItemList(
            offset=offset, limit=limit, total=len(self.all_items), items=items
        )

        end_time = time.time()
        self.call_history.append(
            {
                "offset": offset,
                "limit": limit,
                "returned_count": len(items),
                "duration": end_time - start_time,
            }
        )

        return result


@pytest.mark.asyncio
async def test_basic_merge():
    """Test basic merge functionality"""
    source1 = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
            ("Track C", 80),
        ],
        "source1",
        call_delay=0.05,
    )

    source2 = MockDataSource(
        [
            ("Track X", 95),
            ("Track Y", 85),
            ("Track Z", 75),
        ],
        "source2",
        call_delay=0.05,
    )

    result = await k_way_merge_browse_items(
        [source1, source2], compared_value, offset=0, limit=10
    )

    # Verify results are in descending timestamp order
    expected_timestamps = [100, 95, 90, 85, 80, 75]
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 6

    # Verify each source was called at least once
    assert source1.call_count >= 1
    assert source2.call_count >= 1


@pytest.mark.asyncio
async def test_parallel_initial_fetch():
    """Test that initial fetches are done in parallel"""
    # Create sources with longer delays
    source1 = MockDataSource([("Track A", 100)], "source1", call_delay=0.2)
    source2 = MockDataSource([("Track B", 90)], "source2", call_delay=0.2)
    source3 = MockDataSource([("Track C", 80)], "source3", call_delay=0.2)

    start_time = time.time()
    result = await k_way_merge_browse_items(
        [source1, source2, source3], compared_value, offset=0, limit=10
    )
    end_time = time.time()

    # If done sequentially, would take 0.6+ seconds
    # If done in parallel, should take ~0.2 seconds
    assert (
        end_time - start_time < 0.4
    ), f"Took {end_time - start_time:.2f}s, expected < 0.4s"

    # Verify all items are returned in correct order
    expected_timestamps = [100, 90, 80]
    actual_timestamps = [item.timestamp for item in result.items]
    assert actual_timestamps == expected_timestamps


@pytest.mark.asyncio
async def test_offset_and_limit():
    """Test offset and limit functionality"""
    source1 = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
            ("Track C", 80),
            ("Track D", 70),
        ],
        "source1",
        call_delay=0.01,
    )

    source2 = MockDataSource(
        [
            ("Track X", 95),
            ("Track Y", 85),
            ("Track Z", 75),
            ("Track W", 65),
        ],
        "source2",
        call_delay=0.01,
    )

    # Test with offset=2, limit=3
    result = await k_way_merge_browse_items(
        [source1, source2], compared_value, offset=2, limit=3
    )

    # Should skip first 2 items (100, 95) and return next 3 (90, 85, 80)
    expected_timestamps = [90, 85, 80]
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 3


@pytest.mark.asyncio
async def test_chunking_and_adaptive_sizing():
    """Test that chunking works correctly with adaptive sizing"""
    # Create a large dataset that will require multiple fetches
    large_items = [(f"Track {i}", 1000 - i) for i in range(150)]
    source1 = MockDataSource(large_items, "source1", call_delay=0.01)

    result = await k_way_merge_browse_items(
        [source1], compared_value, offset=0, limit=50
    )

    # Should get first 50 items in descending order
    expected_timestamps = list(range(1000, 950, -1))
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 50

    # Should have made minimal API calls due to adaptive chunking
    print(f"Made {source1.call_count} API calls for 50 items from 150 total")
    assert source1.call_count <= 3, f"Expected <= 3 calls, got {source1.call_count}"


@pytest.mark.asyncio
async def test_exhausted_sources():
    """Test handling of exhausted sources"""
    # Small source that will be exhausted
    small_source = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
        ],
        "small",
        call_delay=0.01,
    )

    # Large source
    large_source = MockDataSource(
        [
            ("Track X", 95),
            ("Track Y", 85),
            ("Track Z", 75),
            ("Track W", 65),
            ("Track V", 55),
        ],
        "large",
        call_delay=0.01,
    )

    result = await k_way_merge_browse_items(
        [small_source, large_source], compared_value, offset=0, limit=10
    )

    # Should get all items merged correctly
    expected_timestamps = [100, 95, 90, 85, 75, 65, 55]
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 7


@pytest.mark.asyncio
async def test_empty_sources():
    """Test handling of empty sources"""
    empty_source = MockDataSource([], "empty", call_delay=0.01)
    normal_source = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
        ],
        "normal",
        call_delay=0.01,
    )

    result = await k_way_merge_browse_items(
        [empty_source, normal_source], compared_value, offset=0, limit=10
    )

    expected_timestamps = [100, 90]
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 2


@pytest.mark.asyncio
async def test_all_empty_sources():
    """Test handling when all sources are empty"""
    empty1 = MockDataSource([], "empty1", call_delay=0.01)
    empty2 = MockDataSource([], "empty2", call_delay=0.01)

    result = await k_way_merge_browse_items(
        [empty1, empty2], compared_value, offset=0, limit=10
    )

    assert len(result.items) == 0


@pytest.mark.asyncio
async def test_single_source():
    """Test with a single data source"""
    source = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
            ("Track C", 80),
        ],
        "single",
        call_delay=0.01,
    )

    result = await k_way_merge_browse_items([source], compared_value, offset=0, limit=2)

    expected_timestamps = [100, 90]
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 2


@pytest.mark.asyncio
async def test_missing_timestamps():
    """Test handling of items with missing timestamps"""
    # Manually create the source to control timestamps
    source = MockDataSource([], "test", call_delay=0.01)

    # Create items manually to test None timestamps
    # Order them by timestamp descending as they would appear in a real source
    item_a = create_browse_item("Track A", 100, "test")  # Highest timestamp
    item_c = create_browse_item("Track C", 80, "test")  # Middle timestamp
    item_b = create_browse_item("Track B", 50, "test")  # Will be set to None

    # Set timestamp to None for one item
    item_b.timestamp = 0

    # Store in timestamp descending order (as data sources should)
    source.all_items = [item_a, item_c, item_b]

    result = await k_way_merge_browse_items(
        [source], compared_value, offset=0, limit=10
    )

    # Items with None timestamp should be treated as 0, so should come last
    # Expected order: Track A (100), Track C (80), Track B (None->0)
    expected_names = ["Track A", "Track C", "Track B"]
    actual_names = [item.name for item in result.items]

    assert actual_names == expected_names


@pytest.mark.asyncio
async def test_call_count_optimization():
    """Test that the number of API calls is optimized"""
    # Create multiple sources with moderate amounts of data
    source1 = MockDataSource(
        [(f"A{i}", 1000 - i) for i in range(100)], "source1", call_delay=0.01
    )
    source2 = MockDataSource(
        [(f"B{i}", 999 - i) for i in range(100)], "source2", call_delay=0.01
    )
    source3 = MockDataSource(
        [(f"C{i}", 998 - i) for i in range(100)], "source3", call_delay=0.01
    )

    # Request a reasonable amount of data
    result = await k_way_merge_browse_items(
        [source1, source2, source3], compared_value, offset=0, limit=30
    )

    assert len(result.items) == 30

    # Verify the calls are optimized - should not be too many
    total_calls = source1.call_count + source2.call_count + source3.call_count
    print(f"Total API calls: {total_calls}")
    print(f"Source 1 calls: {source1.call_count}")
    print(f"Source 2 calls: {source2.call_count}")
    print(f"Source 3 calls: {source3.call_count}")

    # With smart chunking, should need very few calls
    assert total_calls <= 6, f"Expected <= 6 total calls, got {total_calls}"

    # Verify results are properly merged
    timestamps = [item.timestamp for item in result.items]
    assert timestamps == sorted(
        timestamps, reverse=True
    ), "Results should be in descending order"


if __name__ == "__main__":
    # Run tests when script is executed directly
    pytest.main([__file__, "-v"])


async def _silent_source(offset: int, limit: int):
    """A module that leaves the SDK's default body alone answers None."""


@pytest.mark.asyncio
async def test_source_answering_none_counts_as_empty():
    source = MockDataSource([("Track A", 100), ("Track B", 90)], "s", call_delay=0)

    result = await k_way_merge_browse_items(
        [_silent_source, source], compared_value, offset=0, limit=10
    )

    assert [item.name for item in result.items] == ["Track A", "Track B"]
    assert result.total == 2


@pytest.mark.asyncio
async def test_only_sources_answering_none():
    result = await k_way_merge_browse_items(
        [_silent_source], compared_value, offset=0, limit=10
    )

    assert result.items == []
    assert result.total == 0


# Tests for flat_merge function
@pytest.mark.asyncio
async def test_flat_merge_basic():
    """Test basic flat merge functionality - simple concatenation without sorting"""
    source1 = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
        ],
        "source1",
        call_delay=0.01,
    )

    source2 = MockDataSource(
        [
            ("Track X", 95),
            ("Track Y", 85),
        ],
        "source2",
        call_delay=0.01,
    )

    result = await flat_merge([source1, source2], offset=0, limit=10)

    # Should get all items from source1 first, then all items from source2
    # Order should be preserved as returned by each source (no sorting)
    expected_names = ["Track A", "Track B", "Track X", "Track Y"]
    actual_names = [item.name for item in result.items]

    assert actual_names == expected_names
    assert len(result.items) == 4
    assert result.total == 4  # Sum of totals from both sources (2 + 2)
    assert result.offset == 0
    assert result.limit == 10


@pytest.mark.asyncio
async def test_flat_merge_preserves_source_order():
    """Test that flat merge preserves the order within each source"""
    # Create sources with timestamps NOT in descending order to verify no sorting happens
    source1 = MockDataSource(
        [
            ("Track A", 50),  # Lower timestamp first
            ("Track B", 100),  # Higher timestamp second
        ],
        "source1",
        call_delay=0.01,
    )

    source2 = MockDataSource(
        [
            ("Track X", 30),  # Even lower timestamp first
            ("Track Y", 200),  # Highest timestamp second
        ],
        "source2",
        call_delay=0.01,
    )

    result = await flat_merge([source1, source2], offset=0, limit=10)

    # Should preserve exact order from each source, no sorting by timestamp
    expected_timestamps = [50, 100, 30, 200]  # Source1 items, then Source2 items
    actual_timestamps = [item.timestamp for item in result.items]

    assert actual_timestamps == expected_timestamps
    assert len(result.items) == 4


@pytest.mark.asyncio
async def test_flat_merge_parallel_execution():
    """Test that flat merge executes source calls in parallel"""
    # Create sources with longer delays to test parallelism
    source1 = MockDataSource([("Track A", 100)], "source1", call_delay=0.2)
    source2 = MockDataSource([("Track B", 90)], "source2", call_delay=0.2)
    source3 = MockDataSource([("Track C", 80)], "source3", call_delay=0.2)

    start_time = time.time()
    result = await flat_merge([source1, source2, source3], offset=0, limit=10)
    end_time = time.time()

    # If done sequentially, would take 0.6+ seconds
    # If done in parallel, should take ~0.2 seconds
    assert (
        end_time - start_time < 0.4
    ), f"Took {end_time - start_time:.2f}s, expected < 0.4s for parallel execution"

    # Verify all items are included in source order
    expected_names = ["Track A", "Track B", "Track C"]
    actual_names = [item.name for item in result.items]
    assert actual_names == expected_names


@pytest.mark.asyncio
async def test_flat_merge_with_offset_and_limit():
    """Test flat merge with offset and limit parameters"""
    source1 = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
            ("Track C", 80),
        ],
        "source1",
        call_delay=0.01,
    )

    source2 = MockDataSource(
        [
            ("Track X", 95),
            ("Track Y", 85),
        ],
        "source2",
        call_delay=0.01,
    )

    # Test with specific offset and limit
    result = await flat_merge([source1, source2], offset=1, limit=3)

    # Each source should be called with offset=1, limit=3
    # So we should get Track B, Track C from source1 and Track Y from source2
    expected_names = ["Track B", "Track C", "Track Y"]
    actual_names = [item.name for item in result.items]

    assert actual_names == expected_names
    assert result.offset == 1
    assert result.limit == 3
    assert result.total == 5  # Still sum of all totals (3 + 2)


@pytest.mark.asyncio
async def test_flat_merge_empty_sources():
    """Test flat merge with empty sources"""
    empty_source = MockDataSource([], "empty", call_delay=0.01)
    normal_source = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
        ],
        "normal",
        call_delay=0.01,
    )

    result = await flat_merge([empty_source, normal_source], offset=0, limit=10)

    expected_names = ["Track A", "Track B"]
    actual_names = [item.name for item in result.items]

    assert actual_names == expected_names
    assert len(result.items) == 2
    assert result.total == 2  # 0 + 2


@pytest.mark.asyncio
async def test_flat_merge_all_empty_sources():
    """Test flat merge when all sources are empty"""
    empty1 = MockDataSource([], "empty1", call_delay=0.01)
    empty2 = MockDataSource([], "empty2", call_delay=0.01)

    result = await flat_merge([empty1, empty2], offset=0, limit=10)

    assert len(result.items) == 0
    assert result.total == 0
    assert result.offset == 0
    assert result.limit == 10


@pytest.mark.asyncio
async def test_flat_merge_single_source():
    """Test flat merge with a single data source"""
    source = MockDataSource(
        [
            ("Track A", 100),
            ("Track B", 90),
            ("Track C", 80),
        ],
        "single",
        call_delay=0.01,
    )

    result = await flat_merge([source], offset=0, limit=10)

    expected_names = ["Track A", "Track B", "Track C"]
    actual_names = [item.name for item in result.items]

    assert actual_names == expected_names
    assert len(result.items) == 3
    assert result.total == 3


@pytest.mark.asyncio
async def test_flat_merge_total_count_calculation():
    """Test that total count is correctly calculated as sum of all source totals"""
    # Create sources with different numbers of items
    source1 = MockDataSource(
        [(f"A{i}", 100 - i) for i in range(5)],  # 5 items
        "source1",
        call_delay=0.01,
    )

    source2 = MockDataSource(
        [(f"B{i}", 200 - i) for i in range(3)],  # 3 items
        "source2",
        call_delay=0.01,
    )

    source3 = MockDataSource(
        [(f"C{i}", 300 - i) for i in range(7)],  # 7 items
        "source3",
        call_delay=0.01,
    )

    result = await flat_merge([source1, source2, source3], offset=0, limit=20)

    # Should have all items: 5 + 3 + 7 = 15
    assert len(result.items) == 15
    assert result.total == 15

    # Verify source order is preserved
    # First 5 should be from source1 (A0, A1, A2, A3, A4)
    # Next 3 should be from source2 (B0, B1, B2)
    # Last 7 should be from source3 (C0, C1, C2, C3, C4, C5, C6)
    first_five_names = [item.name for item in result.items[:5]]
    next_three_names = [item.name for item in result.items[5:8]]
    last_seven_names = [item.name for item in result.items[8:]]

    assert all(name.startswith("A") for name in first_five_names)
    assert all(name.startswith("B") for name in next_three_names)
    assert all(name.startswith("C") for name in last_seven_names)


@pytest.mark.asyncio
async def test_flat_merge_preserves_exact_order():
    """Test that flat merge preserves exact order from sources regardless of timestamps"""
    source = MockDataSource([], "test", call_delay=0.01)

    # Create items with different timestamp values to verify no sorting happens
    item_a = create_browse_item("Track A", 100, "test")
    item_b = create_browse_item("Track B", 50, "test")  # Lower timestamp
    item_c = create_browse_item("Track C", 75, "test")  # Middle timestamp

    # Order them in a specific sequence (not sorted by timestamp)
    source.all_items = [item_a, item_b, item_c]

    result = await flat_merge([source], offset=0, limit=10)

    # Should preserve exact order from source, regardless of timestamps
    expected_names = ["Track A", "Track B", "Track C"]
    actual_names = [item.name for item in result.items]

    assert actual_names == expected_names
    assert len(result.items) == 3


@pytest.mark.asyncio
async def test_flat_merge_source_answering_none():
    source = MockDataSource([("Track A", 100)], "s", call_delay=0)

    result = await flat_merge([_silent_source, source], offset=0, limit=10)

    assert [item.name for item in result.items] == ["Track A"]
    assert result.total == 1
