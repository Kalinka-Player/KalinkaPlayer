import asyncio
import json
import logging
import os
from collections import defaultdict

from kalinka_eventbus.bus import EventBus
from kalinka_plugin_sdk.events import (
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)
from kalinka_plugin_sdk.inputmodule import InputModule, TrackInfo
from kalinka_plugin_sdk.api import PlayQueueController

logger = logging.getLogger(__name__.split(".")[-1])

STATE_FILE = "kalinka_state.json"


def set_state_file(file_path: str):
    global STATE_FILE
    STATE_FILE = file_path


async def save_state(
    playqueue_eventbus: EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent],
):
    # Ensure the directory exists
    state_dir = os.path.dirname(STATE_FILE)
    if state_dir:  # Only create directory if path is not empty
        os.makedirs(state_dir, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        snapshot = playqueue_eventbus.get_snapshot()
        json.dump(
            # A plugin's hold on the output ends with the process.
            snapshot.model_dump(exclude={"playback_control"}),
            f,
        )
    logger.info("State saved")


async def restore_state(
    playqueue: PlayQueueController, modules: dict[str, InputModule]
):
    try:
        with open(STATE_FILE, "r") as f:
            state = PlayQueueState.model_validate_json(f.read())

        track_info_cache: dict[str, TrackInfo] = {}
        track_ids_by_source: dict[str, list[str]] = defaultdict(list)

        for track in state.track_list:
            track_ids_by_source[track.id.source].append(track.id.id)

        async def fetch_source_track_infos(source: str, track_ids: list[str]) -> None:
            module = modules.get(source)
            if module is None:
                logger.warning(
                    "Skipping restore for source '%s': module is not available",
                    source,
                )
                return

            # De-duplicate to avoid repeated module calls for duplicated queue entries.
            unique_track_ids = list(dict.fromkeys(track_ids))

            try:
                track_infos = await module.get_track_info(unique_track_ids)
            except Exception as e:
                logger.warning(
                    "Failed to retrieve track infos from source '%s': %s",
                    source,
                    e,
                )
                return

            for track_info in track_infos:
                track_info_cache[track_info.id.to_string] = track_info

            missing_count = sum(
                1
                for track_id in unique_track_ids
                if f"kalinka:{source}:track:{track_id}" not in track_info_cache
            )
            if missing_count:
                logger.warning(
                    "Source '%s' did not return %d/%d track infos during restore",
                    source,
                    missing_count,
                    len(unique_track_ids),
                )

        awaitables = [
            fetch_source_track_infos(source, track_ids)
            for source, track_ids in track_ids_by_source.items()
        ]
        if awaitables:
            await asyncio.gather(*awaitables)

        async def track_info_retriever(entity_id) -> TrackInfo:
            """Retrieve TrackInfo from pre-fetched restore cache."""
            track_info = track_info_cache.get(entity_id.to_string)
            if track_info is None:
                raise ValueError(
                    f"Track {entity_id.id} not found in module {entity_id.source}"
                )

            return track_info

        await playqueue.restore_from_state(state, track_info_retriever)
        logger.info("State restored")
    except FileNotFoundError:
        logger.info("No state file found")
        return {}
    except json.JSONDecodeError:
        logger.error("Failed to decode state file")
        return {}
    except Exception as e:
        logger.error(f"Failed to restore state: {e}")
        return {}
