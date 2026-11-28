"""Renderer state messages -> plain dicts, and how they merge.

The renderer reports state the way the native player does: a full snapshot when
a session opens or is asked for one, then discrete changes. Both land in one
dict per session, so a caller never has to reassemble the two shapes. Enum
values become their lowercase names ("stopped", "http_stream") — the ordinals
are protocol constants and are not meaningful outside the wire.

Playback state is replaced, not merged: PlaybackStateChanged restates the
format, the device and the signal path every time. What still accumulates is
what no playback state speaks for — volume, the selected device, the current
source and the queued tokens.

PlaybackEvent converts here too but is never merged: it happened, it states
nothing.
"""

from __future__ import annotations

import base64
import re
from enum import Enum
from typing import Any, Optional

from google.protobuf.descriptor import EnumDescriptor, FieldDescriptor

from .renderer_proto import renderer_pb2 as pb


class StateChange(str, Enum):
    """One state message, by kind.

    The values are the protobuf oneof payload names, so the wire vocabulary
    ends here: everything downstream compares enum members and never spells a
    payload name again.
    """

    SNAPSHOT = "state_snapshot"
    PLAYBACK = "playback_state_changed"
    SOURCE = "source_changed"
    VOLUME = "volume_changed"
    ERROR = "playback_error"

    @classmethod
    def for_payload(cls, payload: str) -> Optional["StateChange"]:
        """The change a payload name denotes, or None if it carries no state."""
        try:
            return cls(payload)
        except ValueError:
            return None


def empty_state() -> dict:
    return {
        "playback_state": "unspecified",
        "source_token": None,
        "current_source": None,
        "format": None,
        "device_info": None,
        "signal_path": None,
        "duration_ms": None,
        "position_ms": 0,
        "position_valid": False,
        "interrupted": False,
        "volume": None,
        "selected_device_id": None,
        "error": None,
        "queued_source_tokens": [],
        "updated_at_unix_ms": 0,
    }


def enum_name(enum_type, value: int, prefix: str) -> str:
    try:
        return enum_type.Name(value).removeprefix(prefix).lower()
    except ValueError:
        return "unknown"


def source_to_dict(source) -> dict:
    return {
        "uri": source.uri,
        "mime_type": source.mime_type,
        "source_token": source.source_token,
    }


def audio_format_to_dict(fmt) -> dict:
    return {
        "sample_rate_hz": fmt.sample_rate_hz,
        "channels": fmt.channels,
        "bits_per_sample": fmt.bits_per_sample,
        "sample_format": fmt.sample_format,
    }


def device_info_to_dict(info) -> dict:
    return {
        "format": audio_format_to_dict(info.format),
        "access": enum_name(pb.DeviceAccess, info.access, "DEVICE_ACCESS_"),
    }


def signal_path_to_dict(path) -> dict:
    return {
        "steps": [
            {
                "kind": step.kind,
                "name": step.name,
                "summary": step.summary,
                "bit_perfect": step.bit_perfect,
            }
            for step in path.steps
        ],
        "latency_ms": path.latency_ms,
    }


def playback_event_to_dict(event) -> Optional[dict]:
    """None for a kind this server does not know, as a newer renderer's is."""
    kind = event.WhichOneof("kind")
    if kind is None:
        return None
    return {
        "source_token": (
            event.source_token if event.HasField("source_token") else None
        ),
        "at_unix_ms": event.at_unix_ms,
        "kind": kind,
        "details": message_to_dict(getattr(event, kind)),
    }


def message_to_dict(message) -> dict:
    """Every field of any message, in plain Python types.

    An unset field with presence (a message, an ``optional`` or a oneof
    member) is None; any other unset field is its default. 64-bit integers
    stay ints, where json_format would make them strings; enums become their
    lowercase names, as everywhere in this module, but a value this server
    does not know stays a number. Bytes become base64, as in proto3's JSON.
    """
    return {
        field.name: _field_to_python(message, field)
        for field in message.DESCRIPTOR.fields
    }


def _field_to_python(message, field: FieldDescriptor) -> Any:
    value = getattr(message, field.name)
    if field.message_type is not None and field.message_type.GetOptions().map_entry:
        value_field = field.message_type.fields_by_name["value"]
        return {key: _to_python(value_field, item) for key, item in value.items()}
    if field.is_repeated:
        return [_to_python(field, item) for item in value]
    if field.has_presence and not message.HasField(field.name):
        return None
    return _to_python(field, value)


def _to_python(field: FieldDescriptor, value) -> Any:
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return message_to_dict(value)
    if field.type == FieldDescriptor.TYPE_ENUM:
        return _enum_value_name(field.enum_type, value)
    if field.type == FieldDescriptor.TYPE_BYTES:
        return base64.b64encode(value).decode("ascii")
    return value


def _enum_value_name(enum_type: EnumDescriptor, number: int) -> Any:
    value = enum_type.values_by_number.get(number)
    if value is None:
        return number
    prefix = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", enum_type.name).upper() + "_"
    return value.name.removeprefix(prefix).lower()


def volume_to_dict(volume) -> dict:
    return {
        "supported": volume.supported,
        "current": volume.current,
        "max": volume.max,
        "backend": enum_name(pb.VolumeBackend, volume.backend, "VOLUME_BACKEND_"),
        "db_values": list(volume.db_values),
        "control_mode": volume.control_mode,
    }


def error_to_dict(error) -> dict:
    return {
        "source": enum_name(pb.ErrorSource, error.source, "ERROR_SOURCE_"),
        "message": error.message,
        "source_token": error.source_token if error.HasField("source_token") else None,
    }


def snapshot_to_dict(snapshot) -> dict:
    source = (
        source_to_dict(snapshot.current_source)
        if snapshot.HasField("current_source")
        else None
    )
    return {
        "playback_state": enum_name(
            pb.PlaybackState, snapshot.playback_state, "PLAYBACK_STATE_"
        ),
        "source_token": source["source_token"] if source else None,
        "current_source": source,
        "format": (
            audio_format_to_dict(snapshot.format)
            if snapshot.HasField("format")
            else None
        ),
        "device_info": (
            device_info_to_dict(snapshot.device_info)
            if snapshot.HasField("device_info")
            else None
        ),
        "signal_path": (
            signal_path_to_dict(snapshot.signal_path)
            if snapshot.HasField("signal_path")
            else None
        ),
        "duration_ms": (
            snapshot.duration_ms if snapshot.HasField("duration_ms") else None
        ),
        "position_ms": snapshot.position_ms,
        "position_valid": snapshot.position_valid,
        "interrupted": False,
        "volume": volume_to_dict(snapshot.volume),
        "selected_device_id": (
            snapshot.selected_device_id
            if snapshot.HasField("selected_device_id")
            else None
        ),
        "error": error_to_dict(snapshot.error) if snapshot.HasField("error") else None,
        "queued_source_tokens": list(snapshot.queued_source_tokens),
        "updated_at_unix_ms": snapshot.captured_at_unix_ms,
    }


def apply(state: dict, change: StateChange, message) -> dict:
    """Merge one state message into `state`, returning the updated dict."""
    if change is StateChange.SNAPSHOT:
        return snapshot_to_dict(message)

    if change is StateChange.PLAYBACK:
        state["playback_state"] = enum_name(
            pb.PlaybackState, message.state, "PLAYBACK_STATE_"
        )
        state["position_ms"] = message.position_ms
        state["position_valid"] = message.position_valid
        state["interrupted"] = message.interrupted
        state["live_ready"] = message.live_ready
        state["live_source_frame"] = (
            message.live_source_frame if message.HasField("live_source_frame") else None
        )
        _set_source_token(
            state,
            message.source_token if message.HasField("source_token") else None,
        )
        state["error"] = (
            error_to_dict(message.error) if message.HasField("error") else None
        )
        state["format"] = (
            audio_format_to_dict(message.format)
            if message.HasField("format")
            else None
        )
        state["device_info"] = (
            device_info_to_dict(message.device_info)
            if message.HasField("device_info")
            else None
        )
        state["signal_path"] = (
            signal_path_to_dict(message.signal_path)
            if message.HasField("signal_path")
            else None
        )
        state["duration_ms"] = (
            message.duration_ms if message.HasField("duration_ms") else None
        )
        state["updated_at_unix_ms"] = message.at_unix_ms
    elif change is StateChange.SOURCE:
        _set_source_token(state, message.source_token)
        # Nothing decoded for the new source yet; the old one's is not its.
        state["format"] = None
        state["signal_path"] = None
        state["duration_ms"] = None
        state["updated_at_unix_ms"] = message.at_unix_ms
    elif change is StateChange.VOLUME:
        state["volume"] = volume_to_dict(message.volume)
    elif change is StateChange.ERROR:
        state["error"] = error_to_dict(message.error)
    return state


def _set_source_token(state: dict, token: Optional[str]) -> None:
    """Events carry only the token; the full descriptor came in a snapshot."""
    state["source_token"] = token
    current: Any = state.get("current_source")
    if current is not None and current.get("source_token") != token:
        state["current_source"] = None
