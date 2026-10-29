import json
import logging
import os

from kalinka_eventbus.bus import EventBus
from kalinka_plugin_sdk.events import (
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
)
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


async def restore_state(playqueue: PlayQueueController):
    try:
        with open(STATE_FILE, "r") as f:
            state = PlayQueueState.model_validate_json(f.read())

        await playqueue.restore_from_state(state)
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
