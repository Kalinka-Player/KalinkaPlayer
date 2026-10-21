"""ReplayGain follows the active renderer's volume owner, including software."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from kalinka_plugin_sdk.api import ReplayEvent
from kalinka_plugin_sdk.datamodel import (
    DeviceVolume,
    PlaybackState,
    PlayerStateEnum,
    VolumeBackend,
)
from kalinka_plugin_sdk.events import PlaybackStateChangedEvent, PlayQueueEventType
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice, SupportedFunction
from kalinka_plugin_sdk.ext_device_events import ExtDeviceEventType

from .renderer_output_device import RendererVolumeDevice

logger = logging.getLogger(__name__.split(".")[-1])

# Fixed output ignores volume changes, so only these modes can carry ReplayGain.
ADJUSTABLE_VOLUME_MODES = ("auto", "hardware", "software")


def volume_curve(volume: DeviceVolume) -> list[float] | None:
    """Return dB at each level, accepting only finite, monotonic calibrations.

    None for a renderer's card mixer that reports no dB scale: how far a step
    moves it is unknown, and a guess can be several times too strong.
    """
    values = volume.db_values
    if (
        len(values) == volume.max_volume + 1
        and all(math.isfinite(v) for v in values)
        and all(a <= b for a, b in zip(values, values[1:]))
    ):
        return values
    if volume.backend == VolumeBackend.HARDWARE and volume.control_mode:
        return None
    exponent = 60 if volume.backend == VolumeBackend.SOFTWARE else 20
    return [-200.0] + [
        exponent * math.log10(i / volume.max_volume)
        for i in range(1, volume.max_volume + 1)
    ]


def shifted_volume(level: int, gain: float, curve: list[float]) -> int:
    """Apply dB on the device's scale, preserving mute and clamping its range."""
    if level <= 0:
        return 0
    if gain == 0:
        return min(level, len(curve) - 1)
    target = curve[min(level, len(curve) - 1)] + gain
    return min(range(len(curve)), key=lambda i: abs(curve[i] - target))


@dataclass
class _Output:
    """Keep renderer volume reads and writes bound to their original owner."""

    renderer_id: str
    device: ExternalOutputDevice

    async def volume(self) -> DeviceVolume:
        if isinstance(self.device, RendererVolumeDevice):
            return await self.device.get_volume_for(self.renderer_id)
        return (await self.device.get_volume()).model_copy(deep=True)

    async def set_volume(self, level: int) -> None:
        if isinstance(self.device, RendererVolumeDevice):
            await self.device.set_volume_for(self.renderer_id, level)
        else:
            await self.device.set_volume(level)


@dataclass
class _Correction:
    """The baseline and applied level for one track on one output."""

    output: _Output
    key: tuple
    baseline: int
    corrected: int
    curve: list[float]

    def restored(self, current: int) -> int:
        if current == self.corrected or (
            0 < current < len(self.curve)
            and self.corrected > 0
            and self.curve[current] == self.curve[self.corrected]
        ):
            # Coarse mixers can echo a different percentage for the same dB.
            return self.baseline
        # Preserve a user's change, including mute, while removing our offset.
        delta = self.curve[self.baseline] - self.curve[self.corrected]
        return shifted_volume(current, delta, self.curve)


class ReplayGainController:
    """Serialize corrections and restore the original output owner on stop."""

    def __init__(self, registry, prefs, router, playback_bus, device_bus):
        self._registry = registry
        self._prefs = prefs
        self._router = router
        self._playback_bus = playback_bus
        self._device_bus = device_bus
        self._state = PlaybackState(state=PlayerStateEnum.STOPPED)
        self._applied: _Correction | None = None
        self._unscaled_reported: set[str] = set()
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._playback_events()),
            asyncio.create_task(self._volume_events()),
        ]

    async def shutdown(self) -> None:
        async with self._lock:
            for task in self._tasks:
                task.cancel()
            self._state = PlaybackState(state=PlayerStateEnum.STOPPED)
            await self._update_safely()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    @asynccontextmanager
    async def changing_volume_mode(
        self, renderer_id: str, mode: str, stop: Callable[[], Awaitable[None]]
    ):
        """Remove the offset before a mode change can make volume unwritable.

        `stop` silences the renderer first, as an offset removed while the
        track still plays is heard as a jump. A connected device keeps its
        offset: the renderer's mode never reaches it, and playback through it
        goes on.
        """
        async with self._lock:
            applied = self._applied
            if (
                applied
                and applied.output.renderer_id == renderer_id
                and not self._prefs.volume_control(renderer_id)
                and (await applied.output.volume()).control_mode != mode
            ):
                await stop()
                await self._restore()
            yield

    async def _playback_events(self) -> None:
        async with self._playback_bus.stream(
            [PlayQueueEventType.PlaybackStateChanged]
        ) as stream:
            async for event in stream:
                if isinstance(event, PlaybackStateChangedEvent):
                    await self.handle(event.state)
                elif isinstance(event, ReplayEvent):
                    await self.handle(event.state.playback_state)

    async def _volume_events(self) -> None:
        async with self._device_bus.stream(
            [ExtDeviceEventType.VolumeChanged]
        ) as stream:
            async for _ in stream:
                await self.refresh()

    async def handle(self, state: PlaybackState) -> None:
        async with self._lock:
            self._state = state
            await self._update_safely()

    async def refresh(self) -> None:
        async with self._lock:
            await self._update_safely()

    async def _update_safely(self) -> None:
        try:
            await self._update()
        except Exception:
            logger.exception(
                "ReplayGain volume adjustment failed; will retry on the next state change"
            )

    async def _restore(self) -> bool:
        applied = self._applied
        if applied is None:
            return True
        volume = await applied.output.volume()
        if not volume.supported:
            if volume.control_mode == "fixed":
                self._applied = None
                return True
            return False
        restored = applied.restored(volume.current_volume)
        await applied.output.set_volume(restored)
        self._applied = None
        logger.info(
            "ReplayGain restored renderer %s volume: %s -> %s",
            applied.output.renderer_id,
            volume.current_volume,
            restored,
        )
        return True

    async def _update(self) -> None:
        renderer_id = self._registry.active_id()
        device = self._router.current()
        mode = self._prefs.replaygain(renderer_id) if renderer_id else "disabled"
        track = self._state.current_track
        gain = getattr(
            track,
            "replaygain_album_gain" if mode == "album" else "replaygain_gain",
            None,
        )
        if (
            self._state.state in (PlayerStateEnum.STOPPED, PlayerStateEnum.ERROR)
            or mode == "disabled"
            or track is None
            or gain is None
            or not math.isfinite(gain)
            or device is None
        ):
            await self._restore()
            return
        if self._state.state != PlayerStateEnum.PLAYING:
            if self._applied is not None and (
                self._applied.output.renderer_id != renderer_id
                or self._applied.output.device is not device
            ):
                await self._restore()
            return
        output = _Output(renderer_id, device)
        volume = await output.volume()
        delegated = bool(self._prefs.volume_control(renderer_id))
        if (
            not volume.supported
            or volume.max_volume <= 0
            or SupportedFunction.SET_VOLUME not in device.supported_functions()
            or (
                not delegated
                and volume.control_mode not in ("", *ADJUSTABLE_VOLUME_MODES)
            )
        ):
            await self._restore()
            return
        key = (renderer_id, id(device), mode, str(track.id), self._state.index, gain)
        if self._applied is not None and self._applied.key == key:
            return
        previous = self._applied
        if (
            previous is not None
            and previous.output.renderer_id == renderer_id
            and previous.output.device is device
        ):
            # Gapless transitions must not briefly raise volume to the baseline.
            baseline = previous.restored(volume.current_volume)
        else:
            if not await self._restore():
                return
            volume = await output.volume()
            baseline = volume.current_volume
        if (
            self._registry.active_id() != renderer_id
            or self._router.current() is not device
            or self._prefs.replaygain(renderer_id) != mode
        ):
            return
        curve = volume_curve(volume)
        if curve is None:
            if renderer_id not in self._unscaled_reported:
                self._unscaled_reported.add(renderer_id)
                logger.info(
                    "ReplayGain skipped on renderer %s: its card mixer reports no dB scale",
                    renderer_id,
                )
            await self._restore()
            return
        baseline = max(0, min(baseline, volume.max_volume))
        target = shifted_volume(baseline, gain, curve)
        correction = _Correction(output, key, baseline, target, curve)
        if target != volume.current_volume:
            await output.set_volume(target)
        self._applied = correction
        logger.info(
            "ReplayGain %s applied for %s on renderer %s: %+.2f dB, volume %s -> %s (baseline %s)",
            mode,
            track.id,
            renderer_id,
            gain,
            volume.current_volume,
            target,
            baseline,
        )
