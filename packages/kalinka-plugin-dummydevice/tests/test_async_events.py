"""
Tests for async event handling in DummyDevice
"""

import asyncio
import contextlib
import pytest
from unittest.mock import Mock
from kalinka_plugin_dummydevice.dummydevice import DummyDevice
from kalinka_plugin_sdk.ext_device_events import VolumeChangedEvent


@contextlib.asynccontextmanager
async def running(device):
    """The lifecycle the server gives a device: start(), then shutdown() however the test ends."""
    await device.start()
    try:
        yield device
    finally:
        await device.shutdown()


@pytest.mark.asyncio
async def test_volume_change_event_emitted():
    """Test that volume changes emit events"""
    emitter = Mock()
    device = DummyDevice(emitter)

    async with running(device):
        # Set volume
        await device.set_volume(75)

        # Wait a bit for the event to be processed (debounce time + a bit)
        await asyncio.sleep(0.2)

        # Check that event was dispatched
        assert emitter.dispatch.called
        call_args = emitter.dispatch.call_args[0][0]
        assert isinstance(call_args, VolumeChangedEvent)
        assert call_args.volume.current_volume == 75


@pytest.mark.asyncio
async def test_volume_change_debouncing():
    """Test that rapid volume changes are debounced"""
    emitter = Mock()
    device = DummyDevice(emitter)

    async with running(device):
        # Make multiple rapid volume changes
        await device.set_volume(10)
        await device.set_volume(20)
        await device.set_volume(30)
        await device.set_volume(40)
        await device.set_volume(50)

        # Wait for debounce time
        await asyncio.sleep(0.2)

        # Should have only emitted one event (or very few) with the final value
        assert emitter.dispatch.called
        # Get the last call
        last_call_args = emitter.dispatch.call_args[0][0]
        assert isinstance(last_call_args, VolumeChangedEvent)
        assert last_call_args.volume.current_volume == 50


@pytest.mark.asyncio
async def test_get_volume():
    """Test getting volume"""
    emitter = Mock()
    device = DummyDevice(emitter)

    async with running(device):
        # Default volume should be 50
        volume = await device.get_volume()
        assert volume.current_volume == 50
        assert volume.max_volume == 100

        # Change volume
        await device.set_volume(75)
        volume = await device.get_volume()
        assert volume.current_volume == 75


@pytest.mark.asyncio
async def test_power_control():
    """Test power on/off"""
    emitter = Mock()
    device = DummyDevice(emitter)

    async with running(device):
        # Initially off
        assert not await device.is_power_on()

        # Power on
        await device.power_on()
        assert await device.is_power_on()

        # Power off
        await device.power_off()
        assert not await device.is_power_on()


@pytest.mark.asyncio
async def test_volume_validation():
    """Test that invalid volume values raise errors"""
    emitter = Mock()
    device = DummyDevice(emitter)

    async with running(device):
        # Valid values should work
        await device.set_volume(0)
        await device.set_volume(100)

        # Invalid values should raise
        with pytest.raises(ValueError):
            await device.set_volume(-1)

        with pytest.raises(ValueError):
            await device.set_volume(101)


@pytest.mark.asyncio
async def test_cleanup_on_exit():
    """Test that shutdown stops the event sender"""
    emitter = Mock()
    device = DummyDevice(emitter)

    async with running(device):
        await device.set_volume(50)
        # Task should be running
        assert device._event_sender_task is not None
        assert not device._event_sender_task.done()

    # After shutdown, the task should be finished
    assert device._event_sender_task.done()
    assert device._shutdown
