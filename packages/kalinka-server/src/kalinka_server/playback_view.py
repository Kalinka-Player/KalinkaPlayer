"""How a renderer's playback state is shown to clients.

One mapping for every owner of the output — the play queue and any plugin
playing outside it — so clients read the same vocabulary whoever plays.
"""

from __future__ import annotations

import secrets
from typing import Optional

from kalinka_plugin_sdk.datamodel import (
    AudioInfo,
    DeviceAccess,
    OutputInfo,
    PlayerStateEnum,
    SignalPath,
    SignalStep,
)

from .stream_state import (
    AudioGraphNodeState,
    DeviceAccess as StreamDeviceAccess,
    SignalPath as StreamSignalPath,
    StreamInfo,
    to_source_token,
)

# Picked once per process: a restarted server numbers its streams from 0 again.
_RUN_ID = secrets.token_hex(4)

# A play lasts from its first buffering until it stops, fails or runs out.
_IN_PLAY = (
    AudioGraphNodeState.PREPARING,
    AudioGraphNodeState.STREAMING,
    AudioGraphNodeState.PAUSED,
)

_ACCESS = {
    StreamDeviceAccess.UNKNOWN: DeviceAccess.UNKNOWN,
    StreamDeviceAccess.EXCLUSIVE: DeviceAccess.EXCLUSIVE,
    StreamDeviceAccess.SHARED: DeviceAccess.SHARED,
}


def to_signal_path(path: Optional[StreamSignalPath]) -> Optional[SignalPath]:
    if path is None:
        return None
    return SignalPath(
        steps=[
            SignalStep(kind=step.kind, name=step.name, summary=step.summary)
            for step in path.steps
        ],
        latency_ms=path.latency_ms,
    )


def to_output_info(stream_info: StreamInfo) -> Optional[OutputInfo]:
    if stream_info.device is None:
        return None
    return OutputInfo(
        sample_format=stream_info.device.format.sample_format,
        sample_rate=stream_info.device.format.sample_rate,
        channels=stream_info.device.format.channels,
        bits_per_sample=stream_info.device.format.bits_per_sample,
        access=_ACCESS.get(stream_info.device.access, DeviceAccess.UNKNOWN),
        lossless_path=stream_info.lossless_path,
        signal_path=to_signal_path(stream_info.signal_path),
    )


def to_audio_info(stream_info: StreamInfo):
    if stream_info is None:
        return None
    return AudioInfo(
        sample_format=stream_info.format.sample_format,
        sample_rate=stream_info.format.sample_rate,
        channels=stream_info.format.channels,
        bits_per_sample=stream_info.format.bits_per_sample,
        # The REST AudioInfo has no absent; clients have always read 0 there.
        duration_ms=stream_info.duration_ms or 0,
        output=to_output_info(stream_info),
    )


def token_to_play_id(source_token: Optional[str]) -> Optional[str]:
    """What clients call the play of the stream a renderer names by
    ``source_token``: unlike the token, never the same in another run of the
    server."""
    return None if source_token is None else f"{_RUN_ID}-{source_token}"


def to_play_id(state: AudioGraphNodeState, stream_id: Optional[int]) -> Optional[str]:
    """The play of a stream in ``state``, as :func:`token_to_play_id` names
    it; None once it no longer plays."""
    if stream_id is None or state not in _IN_PLAY:
        return None
    return token_to_play_id(to_source_token(stream_id))


def to_state_name(state: AudioGraphNodeState) -> Optional[PlayerStateEnum]:
    if state == AudioGraphNodeState.ERROR:
        return PlayerStateEnum.ERROR
    elif state == AudioGraphNodeState.STOPPED or state == AudioGraphNodeState.FINISHED:
        return PlayerStateEnum.STOPPED
    elif state == AudioGraphNodeState.PREPARING:
        return PlayerStateEnum.BUFFERING
    elif state == AudioGraphNodeState.STREAMING:
        return PlayerStateEnum.PLAYING
    elif state == AudioGraphNodeState.PAUSED:
        return PlayerStateEnum.PAUSED
    return None
