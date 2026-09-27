"""How a renderer's playback state is shown to clients.

One mapping for every owner of the output — the play queue and any plugin
playing outside it — so clients read the same vocabulary whoever plays.
"""

from __future__ import annotations

from typing import Optional

from kalinka_plugin_sdk.datamodel import (
    AudioInfo,
    DeviceAccess,
    OutputInfo,
    PlayerStateEnum,
)

from .stream_state import (
    AudioGraphNodeState,
    DeviceAccess as StreamDeviceAccess,
    StreamInfo,
)

_ACCESS = {
    StreamDeviceAccess.UNKNOWN: DeviceAccess.UNKNOWN,
    StreamDeviceAccess.EXCLUSIVE: DeviceAccess.EXCLUSIVE,
    StreamDeviceAccess.SHARED: DeviceAccess.SHARED,
}


def to_output_info(stream_info: StreamInfo) -> Optional[OutputInfo]:
    if stream_info.device is None:
        return None
    return OutputInfo(
        sample_rate=stream_info.device.format.sample_rate,
        channels=stream_info.device.format.channels,
        bits_per_sample=stream_info.device.format.bits_per_sample,
        access=_ACCESS.get(stream_info.device.access, DeviceAccess.UNKNOWN),
        lossless_path=stream_info.lossless_path,
    )


def to_audio_info(stream_info: StreamInfo):
    if stream_info is None:
        return None
    return AudioInfo(
        sample_rate=stream_info.format.sample_rate,
        channels=stream_info.format.channels,
        bits_per_sample=stream_info.format.bits_per_sample,
        # The REST AudioInfo has no absent; clients have always read 0 there.
        duration_ms=stream_info.duration_ms or 0,
        output=to_output_info(stream_info),
    )


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
