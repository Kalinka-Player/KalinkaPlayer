"""
Device automation module for Kalinka player.

This module provides automatic device management features such as:
- Automatically turning on the device when playback starts or buffers
- Automatically turning off the device (with a grace period) when playback
  is paused or stopped, also stopping the player for energy saving
"""

import asyncio
import logging
from typing import Optional

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.api import PlayQueueController
from kalinka_plugin_sdk.datamodel import PlayerStateEnum, PlaybackState
from kalinka_plugin_sdk.events import (
    PlayQueueEvent,
    PlayQueueEventType,
    PlayQueueState,
    PlaybackStateChangedEvent,
)
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice, SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    DevicePowerStateChangedEvent,
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
)

from .config_model import DeviceAutomationConfig
from .output_device_router import OutputDeviceRouter

logger = logging.getLogger(__name__.split(".")[-1])


class DeviceAutomation:
    """Handles automatic device control based on playback state changes."""

    def __init__(
        self,
        config: DeviceAutomationConfig,
        playqueue: PlayQueueController,
        playqueue_eventbus: EventBus[
            PlayQueueState, PlayQueueEventType, PlayQueueEvent
        ],
        ext_device_eventbus: EventBus[
            ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent
        ],
        router: Optional[OutputDeviceRouter] = None,
    ):
        """
        Initialize the device automation module.

        Args:
            config: Configuration for device automation features
            playqueue: The playqueue controller to control playback
            playqueue_eventbus: Event bus for playback events
            ext_device_eventbus: Event bus for device events
            router: Resolves which module owns the active renderer's output.
                Consulted per action, so power follows renderer selection and
                delegation.
        """
        self.config = config
        self.playqueue = playqueue
        self.playqueue_eventbus = playqueue_eventbus
        self.ext_device_eventbus = ext_device_eventbus
        self._router = router

        self._last_state: Optional[PlayerStateEnum] = None
        # Pending power-offs, keyed by module. Keyed rather than single because
        # switching renderers leaves the previous device idle while another one
        # starts playing: the old device must still power down, and the new
        # one's playback must not cancel that.
        self._auto_off_tasks: dict[str, asyncio.Task] = {}
        # Module we powered on for the current playback, so it is the one
        # powered off later even if the active renderer has changed since.
        self._in_use: Optional[str] = None
        self._in_use_device: Optional[ExternalOutputDevice] = None
        self._subscription = None
        self._stream_task: Optional[asyncio.Task] = None
        self._device_stream_task: Optional[asyncio.Task] = None
        # Set when device powers off externally; suppresses the auto-off timer until
        # the device is known to be on again.
        self._device_externally_off: bool = False

    @property
    def device(self) -> Optional[ExternalOutputDevice]:
        """Whoever owns the active renderer's output right now. Resolved per
        use, never cached: delegation and renderer selection both change it,
        and so do the capabilities that go with it."""
        return self._router.current() if self._router else None

    def _device_name(self) -> str:
        return self._router.current_name() if self._router else ""

    def forget(self, name: str) -> None:
        """Drop every reference to the module ``name``, which is being
        unloaded: its auto-off timer must not power it off through an
        instance that is gone."""
        task = self._auto_off_tasks.pop(name, None)
        if task is not None:
            task.cancel()
        if self._in_use == name:
            self._in_use = self._in_use_device = None

    @staticmethod
    def _can(device: ExternalOutputDevice, function: SupportedFunction) -> bool:
        return function in device.supported_functions()

    async def start(self):
        """Start the device automation module."""
        logger.info("Starting device automation module")
        logger.info(
            f"Configuration: auto_power_on={self.config.auto_power_on}, "
            f"auto_power_off={self.config.auto_power_off}, "
            f"auto_off_timeout_seconds={self.config.auto_off_timeout_seconds}"
        )

        # Start listening to playback events
        self._stream_task = asyncio.create_task(self._event_listener())

        # Start listening to device state changes (e.g. input switch)
        self._device_stream_task = asyncio.create_task(self._device_event_listener())

    async def _device_event_listener(self):
        """Listen for external device state changes (e.g. input switched away)."""
        while True:
            try:
                async with self.ext_device_eventbus.stream(
                    [ExtDeviceEventType.DevicePowerStateChanged]
                ) as stream:
                    async for event in stream:
                        if isinstance(event, DevicePowerStateChangedEvent):
                            if not event.power_on:
                                await self._on_device_power_off()
                            else:
                                self._device_externally_off = False
            except asyncio.CancelledError:
                logger.debug("Device automation device event listener cancelled")
                raise
            except Exception as e:
                logger.error(
                    f"Error in device automation device event listener: {e}",
                    exc_info=True,
                )
                await asyncio.sleep(1)

    async def _on_device_power_off(self):
        """Handle device power-off or input switch — stop playback immediately."""
        logger.info("Device powered off or input switched — stopping playback")
        self._device_externally_off = True
        self._cancel_auto_off_timer()
        try:
            current_state = await self.playqueue.get_playback_state()
            if current_state.state in (
                PlayerStateEnum.PLAYING,
                PlayerStateEnum.BUFFERING,
                PlayerStateEnum.PAUSED,
            ):
                await self.playqueue.stop()
        except Exception as e:
            logger.error(
                f"Failed to stop playback on device power-off: {e}", exc_info=True
            )

    async def _event_listener(self):
        """Listen for playback state changes."""
        while True:
            try:
                async with self.playqueue_eventbus.stream(
                    [PlayQueueEventType.PlaybackStateChanged]
                ) as stream:
                    async for event in stream:
                        if isinstance(event, PlaybackStateChangedEvent):
                            await self._handle_playback_state_change(event.state)
            except asyncio.CancelledError:
                logger.debug("Device automation event listener cancelled")
                raise
            except Exception as e:
                logger.error(
                    f"Error in device automation event listener: {e}", exc_info=True
                )
                await asyncio.sleep(1)

    async def _handle_playback_state_change(self, state: PlaybackState):
        """
        Handle playback state changes.

        Args:
            state: The new playback state
        """
        new_state = state.state
        logger.debug(f"Playback state changed: {self._last_state} -> {new_state}")

        if new_state in (PlayerStateEnum.BUFFERING, PlayerStateEnum.PLAYING):
            self._cancel_auto_off_timer(self._device_name())
            # Only fire _on_active on the *transition* into the active group.
            # Without this guard, BUFFERING → PLAYING (~600 ms apart on a fresh
            # track) triggers power_on twice in quick succession; the first
            # call has often not flipped the receiver fully on yet, so the
            # second is_power_on() check returns False and we redundantly fire
            # setPower=on again.
            if self._last_state not in (
                PlayerStateEnum.BUFFERING,
                PlayerStateEnum.PLAYING,
            ):
                await self._on_active()

        elif new_state in (PlayerStateEnum.PAUSED, PlayerStateEnum.STOPPED):
            name = self._in_use if self._in_use is not None else self._device_name()
            device = (
                self._in_use_device if self._in_use is not None else self.device
            )
            self._start_auto_off_timer(name, device)

        self._last_state = new_state

    async def _on_active(self):
        """Handle transition to an active state (buffering or playing)."""
        self._device_externally_off = False
        name = self._device_name()
        # Playback moved to a device we were not using — the previous one is
        # now idle, so let it power down on its own timer rather than leaving
        # it on forever because something else started playing.
        previous, previous_device = self._in_use, self._in_use_device
        self._in_use, self._in_use_device = name, self.device
        if previous is not None and previous != name:
            self._start_auto_off_timer(previous, previous_device)

        if not self.config.auto_power_on:
            return

        device = self._in_use_device
        if device is None or not self._can(device, SupportedFunction.POWER_ON):
            logger.debug("Device power on not available")
            return

        try:
            if self._can(device, SupportedFunction.IS_POWER_ON):
                is_on = await device.is_power_on()
                if is_on:
                    logger.debug("Device is already powered on")
                    return

            logger.info("Auto power on: Turning on device")
            await device.power_on()

        except Exception as e:
            logger.error(f"Failed to auto power on device: {e}", exc_info=True)

    def _start_auto_off_timer(
        self, name: str, device: Optional[ExternalOutputDevice]
    ):
        """Start the auto-off grace period for one device."""
        if not self.config.auto_power_off or device is None:
            return

        timeout = self.config.auto_off_timeout_seconds
        if timeout <= 0:
            logger.debug("Auto-off timeout disabled")
            return

        # Only for the device currently in charge: one playback has moved away
        # from is idle regardless of why the current one went quiet.
        if self._device_externally_off and name == self._device_name():
            logger.debug("Skipping auto-off timer: device already externally powered off")
            return

        # Don't restart the timer if it's already running (e.g. paused → stopped)
        running = self._auto_off_tasks.get(name)
        if running and not running.done():
            logger.debug("Auto-off timer already running for %s", name)
            return

        logger.debug("Starting auto-off timer for %s (%ss)", name, timeout)
        self._auto_off_tasks[name] = asyncio.create_task(
            self._auto_off_timeout(name, device)
        )

    async def _auto_off_timeout(self, name: str, device: ExternalOutputDevice):
        """Wait for the grace period then stop playback and power off.

        Whether the queue is this device's business is decided here, not when
        the timer started: playback may have moved to another renderer since,
        and this device is then simply idle — it powers down without touching
        a queue that is now somebody else's.
        """
        try:
            await asyncio.sleep(self.config.auto_off_timeout_seconds)

            if name == self._in_use:
                current_state = await self.playqueue.get_playback_state()
                if current_state.state not in (
                    PlayerStateEnum.PAUSED,
                    PlayerStateEnum.STOPPED,
                ):
                    logger.debug(
                        f"Auto-off timer fired but state is {current_state.state}, skipping"
                    )
                    return

                # Stop playback first for energy saving (no-op if stopped)
                if current_state.state == PlayerStateEnum.PAUSED:
                    try:
                        await self.playqueue.stop()
                    except Exception as e:
                        logger.error(
                            f"Failed to stop playback on auto-off: {e}", exc_info=True
                        )

            logger.info(
                "Auto-off timeout reached (%ss), powering off %s",
                self.config.auto_off_timeout_seconds,
                name,
            )
            if self._can(device, SupportedFunction.POWER_OFF):
                try:
                    if self._can(device, SupportedFunction.IS_POWER_ON):
                        if not await device.is_power_on():
                            logger.debug("Device is already powered off")
                            return

                    logger.info("Auto power off: Turning off device")
                    await device.power_off()

                except Exception as e:
                    logger.error(f"Failed to auto power off device: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.debug("Auto-off timer cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in auto-off timeout handler: {e}", exc_info=True)
        finally:
            # Only the bookkeeping: a cancelled timer means playback resumed on
            # this device, so it stays the one in use.
            self._auto_off_tasks.pop(name, None)

    def _cancel_auto_off_timer(self, name: Optional[str] = None):
        """Cancel pending power-offs — one device's, or every one."""
        names = [name] if name is not None else list(self._auto_off_tasks)
        for key in names:
            task = self._auto_off_tasks.pop(key, None)
            if task is not None and not task.done():
                logger.debug("Cancelling auto-off timer for %s", key)
                task.cancel()

    async def shutdown(self):
        """Shutdown the device automation module."""
        logger.info("Shutting down device automation module")

        self._cancel_auto_off_timer()

        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

        if self._device_stream_task:
            self._device_stream_task.cancel()
            try:
                await self._device_stream_task
            except asyncio.CancelledError:
                pass

        logger.info("Device automation module shut down")
