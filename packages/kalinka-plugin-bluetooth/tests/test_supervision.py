import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import kalinka_plugin_bluetooth as bluetooth
from kalinka_plugin_sdk.module_health import ModuleHealthState


@pytest.fixture
def plugin():
    plugin = bluetooth.KalinkaPluginBluetooth()
    plugin.context = SimpleNamespace(
        config=bluetooth.BluetoothConfig(enabled=True),
        live_audio=None,
        direct_playback=None,
    )
    return plugin


async def test_missing_backend_warns_once_keeps_status_and_backs_off(
    plugin, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=bluetooth.__name__)
    backends, delays = [], []

    def make_backend(*args):
        backend = SimpleNamespace(
            connect=AsyncMock(side_effect=RuntimeError("BlueALSA is unavailable")),
            close=Mock(),
        )
        backends.append(backend)
        return backend

    async def wait(delay):
        delays.append(delay)
        state = await plugin.get_state()
        assert state.state == ModuleHealthState.WARNING
        assert "BlueALSA is unavailable" in state.message
        assert plugin.backend is None
        if len(delays) == 7:
            raise asyncio.CancelledError

    monkeypatch.setattr(bluetooth, "BlueAlsaBackend", make_backend)
    monkeypatch.setattr(bluetooth, "monotonic", lambda: 0)
    monkeypatch.setattr(bluetooth.asyncio, "sleep", wait)
    with pytest.raises(asyncio.CancelledError):
        await plugin._run()

    assert delays == [2, 4, 8, 16, 30, 30, 30]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "BlueALSA is unavailable" in warnings[0].message
    for backend in backends:
        backend.close.assert_called_once()


async def test_recovery_clears_status_and_new_outage_warns_again(
    plugin, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=bluetooth.__name__)
    backends, delays = [], []
    now = 0.0

    async def events():
        nonlocal now
        yield None
        assert plugin.error is None
        assert (await plugin.get_state()).state == ModuleHealthState.READY
        now += 31  # A stable connection resets the retry budget.
        raise RuntimeError("BlueALSA is unavailable")

    def make_backend(*args):
        healthy = len(backends) == 2
        backend = SimpleNamespace(
            connect=AsyncMock(
                side_effect=None if healthy else RuntimeError("BlueALSA is unavailable")
            ),
            close=Mock(),
            events=events,
            endpoints=lambda: [],
            devices=lambda: {},
            audio_devices=lambda: set(),
            changed=asyncio.Event(),
            adapter="hci0",
            bluez={"/org/bluez/hci0": {bluetooth.ADAPTER: {"Alias": "Kalinka"}}},
        )
        backends.append(backend)
        return backend

    async def wait(delay):
        nonlocal now
        delays.append(delay)
        now += delay
        if len(delays) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(bluetooth, "BlueAlsaBackend", make_backend)
    monkeypatch.setattr(bluetooth, "monotonic", lambda: now)
    monkeypatch.setattr(bluetooth.asyncio, "sleep", wait)
    with pytest.raises(asyncio.CancelledError):
        await plugin._run()

    assert delays == [2, 4, 2, 4]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    assert caplog.messages.count("Bluetooth receiver available again") == 1
    assert "BlueALSA is unavailable" in plugin.error
    for backend in backends:
        backend.close.assert_called_once()


async def test_shutdown_interrupts_retry_backoff(plugin, monkeypatch):
    waiting = asyncio.Event()

    async def wait(delay):
        waiting.set()
        await asyncio.Event().wait()

    backend = SimpleNamespace(
        connect=AsyncMock(side_effect=RuntimeError("BlueALSA is unavailable")),
        close=Mock(),
    )
    monkeypatch.setattr(bluetooth, "BlueAlsaBackend", lambda *args: backend)
    monkeypatch.setattr(bluetooth.asyncio, "sleep", wait)
    plugin.task = asyncio.create_task(plugin._run())
    await asyncio.wait_for(waiting.wait(), 1)
    await asyncio.wait_for(plugin.shutdown(), 1)
    assert plugin.task is None and plugin.backend is None
    backend.close.assert_called_once()
