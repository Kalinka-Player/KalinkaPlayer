"""Unit tests for how requests reach the amplifier: one at a time, and only
the newest of a burst of volume changes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kalinka_plugin_sdk.ext_device import DeviceVolume

from kalinka_plugin_musiccast.config_model import KalinkaPluginMusiccastConfig
from kalinka_plugin_musiccast.musiccast import KalinkaPluginMusiccastDevice


class _Amp:
    """Answers requests only when released, counting how many overlap."""

    def __init__(self):
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.release = asyncio.Event()

    async def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
        return MagicMock(status_code=200, json=lambda: {"response_code": 0})

    def volumes(self) -> list[int]:
        return [int(u.rsplit("=", 1)[1]) for u in self.urls if "setVolume" in u]


@pytest.fixture
def amp():
    return _Amp()


@pytest.fixture
def device(amp, monkeypatch):
    monkeypatch.setattr(KalinkaPluginMusiccastDevice, "_VOLUME_SEND_INTERVAL_SEC", 0)
    monkeypatch.setattr(KalinkaPluginMusiccastDevice, "_ECHO_TIMEOUT_SEC", 0.01)
    config = KalinkaPluginMusiccastConfig(connected_input="netusb", zone_name="main")
    dev = KalinkaPluginMusiccastDevice(config, MagicMock(), MagicMock())
    dev.session = amp
    dev.base_url = "http://amp/YamahaExtendedControl/v1/"
    dev.ready = True
    dev.udp_port = 41100
    dev.volume = DeviceVolume(
        max_volume=100, current_volume=30, volume_gain=0, supported=True
    )
    return dev


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.unit
async def test_only_one_request_reaches_the_amplifier_at_a_time(device, amp):
    requests = [
        asyncio.create_task(device._request_musiccast(f"/main/getStatus?{i}"))
        for i in range(3)
    ]
    await _settle()
    amp.release.set()
    await asyncio.gather(*requests)

    assert len(amp.urls) == 3
    assert amp.max_in_flight == 1


@pytest.mark.unit
async def test_a_volume_burst_sends_the_first_and_the_newest_level(device, amp):
    await device.set_volume(40)
    await _settle()
    # Each returns while the amplifier still holds the first request.
    for level in (41, 42, 43):
        await device.set_volume(level)
    await _settle()
    assert amp.volumes() == [40]

    amp.release.set()
    await device._volume_sender

    assert amp.volumes() == [40, 43]
    assert device.volume.current_volume == 43


@pytest.mark.unit
async def test_volume_waits_behind_another_request(device, amp):
    status = asyncio.create_task(device._request_musiccast("/main/getStatus"))
    await _settle()
    await device.set_volume(50)
    await _settle()
    assert amp.volumes() == []

    amp.release.set()
    await status
    await device._volume_sender
    assert amp.volumes() == [50]


@pytest.mark.unit
async def test_a_failed_send_drops_the_queued_level(device, monkeypatch):
    async def refuse(url, headers=None, timeout=None):
        raise httpx.ConnectError("All connection attempts failed")

    device.session = MagicMock(get=refuse)
    monkeypatch.setattr(device, "rediscover_device", AsyncMock())

    await device.set_volume(60)
    await device.set_volume(61)
    await device._volume_sender

    device.rediscover_device.assert_awaited()
    assert device._volume_target is None


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["read_error", "http_error"])
async def test_failed_mute_invalidates_volume_until_status_is_recovered(device, failure):
    device.config.device_addr = "amp"
    recovering, respond = asyncio.Event(), asyncio.Event()
    status = {
        "response_code": 0,
        "max_volume": 100,
        "volume": 30,
        "power": "on",
        "input": "netusb",
    }

    async def get(url, headers=None, timeout=None):
        if "setVolume" in url:
            if failure == "read_error":
                raise httpx.ReadError("Connection reset by peer")
            return MagicMock(status_code=503)
        recovering.set()
        await respond.wait()
        return MagicMock(status_code=200, json=lambda: status)

    device.session = MagicMock(get=get)
    try:
        await device.set_volume(0)
        await device._volume_sender
        assert not device.ready
        assert not (await device.get_volume()).supported
        assert any(
            not call.args[0].volume.supported
            for call in device.event_emitter.dispatch.call_args_list
            if hasattr(call.args[0], "volume")
        )
        await asyncio.wait_for(recovering.wait(), 1)
    finally:
        respond.set()
        if device._discovery_task is not None:
            await asyncio.wait_for(device._discovery_task, 1)

    assert device.ready
    assert device.volume.supported and device.volume.current_volume == 30
    assert device._volume_target is None and not device._unconfirmed


@pytest.mark.unit
async def test_every_request_names_our_event_port(device, amp):
    amp.release.set()
    await device._get_status()
    await device.set_volume(55)
    await device._volume_sender
    await device.power_off()

    assert len(amp.headers) >= 3
    for headers in amp.headers:
        assert headers == {"X-AppName": "MusicCast/1.0(Linux)", "X-AppPort": "41100"}


@pytest.mark.unit
async def test_no_event_headers_before_a_port_is_chosen(device, amp):
    device.udp_port = None
    amp.release.set()
    await device._get_status()

    assert amp.headers == [{}]


@pytest.mark.unit
async def test_volume_sends_are_paced_and_end_on_the_newest_level(
    device, amp, monkeypatch
):
    """The amplifier answers in milliseconds, so one request in flight alone
    would let every level of a drag through."""
    monkeypatch.setattr(device, "_VOLUME_SEND_INTERVAL_SEC", 0.05)
    loop = asyncio.get_running_loop()
    sent_at: list[float] = []
    original_get = amp.get

    async def timed_get(url, headers=None, timeout=None):
        sent_at.append(loop.time())
        return await original_get(url, headers, timeout)

    amp.get = timed_get
    amp.release.set()

    await device.set_volume(40)
    await _settle()
    for level in range(41, 60):
        await device.set_volume(level)
        await asyncio.sleep(0.005)
    await device._volume_sender

    volumes = amp.volumes()
    assert volumes[0] == 40
    assert volumes[-1] == 59
    assert len(volumes) < 6
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    assert all(gap >= 0.045 for gap in gaps)


def _echo(device, level):
    return device._handle_event({"main": {"volume": level}})


@pytest.mark.unit
async def test_a_stale_echo_does_not_replace_our_newer_level(device, amp, monkeypatch):
    monkeypatch.setattr(device, "_VOLUME_SEND_INTERVAL_SEC", 0.05)
    amp.release.set()
    await device.set_volume(40)
    await _settle()
    await device.set_volume(45)  # queued while the sender paces

    await _echo(device, 40)
    assert device.volume.current_volume == 45
    await _echo(device, 38)  # older than our queued 45: dropped
    assert device.volume.current_volume == 45

    await asyncio.sleep(0.06)
    assert amp.volumes() == [40, 45]
    await _echo(device, 45)
    await device._volume_sender
    await _echo(device, 50)  # nothing of ours outstanding: a real change
    assert device.volume.current_volume == 50


@pytest.mark.unit
async def test_an_echo_confirms_every_send_before_it(device):
    device._unconfirmed.extend([40, 42, 45])
    device.volume.current_volume = 45

    await _echo(device, 42)
    assert list(device._unconfirmed) == [45]
    assert device.volume.current_volume == 45
    assert not device._volume_wake.is_set()

    await _echo(device, 45)
    assert not device._unconfirmed
    assert device._volume_wake.is_set()


@pytest.mark.unit
async def test_a_change_made_elsewhere_shows_when_nothing_of_ours_is_pending(device):
    device._volume_changed_event.clear()
    await _echo(device, 70)
    assert device.volume.current_volume == 70
    assert device._volume_changed_event.is_set()


@pytest.mark.unit
async def test_a_missing_echo_is_reconciled_from_the_amplifier(device):
    class _SilentAmp:
        """Takes the level but never echoes it, and reports its own."""

        async def get(self, url, headers=None, timeout=None):
            body = {"response_code": 0}
            if "getStatus" in url:
                body["volume"] = 33
            return MagicMock(status_code=200, json=lambda: body)

    device.session = _SilentAmp()
    await device.set_volume(50)
    assert device.volume.current_volume == 50
    await device._volume_sender

    assert not device._unconfirmed
    assert device.volume.current_volume == 33


@pytest.mark.unit
async def test_a_new_level_while_waiting_for_echoes_is_sent(device, amp, monkeypatch):
    monkeypatch.setattr(device, "_ECHO_TIMEOUT_SEC", 1.0)
    amp.release.set()
    await device.set_volume(40)
    await _settle()
    await device.set_volume(44)
    await _settle()

    assert amp.volumes() == [40, 44]
    await _echo(device, 44)
    await asyncio.wait_for(device._volume_sender, timeout=0.5)
    assert not device._unconfirmed


@pytest.mark.unit
async def test_losing_the_amplifier_drops_the_queued_level(device, amp, monkeypatch):
    """A failure elsewhere (status poll, power) must not leave a stale level
    to be sent over the one read back on reconnect."""
    monkeypatch.setattr(device, "_VOLUME_SEND_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(device, "_discovery_worker", AsyncMock())
    amp.release.set()
    await device.set_volume(40)
    await _settle()
    await device.set_volume(45)  # queued while the sender paces

    await device.rediscover_device()
    await asyncio.wait_for(device._volume_sender, 0.5)

    assert not device.ready
    assert amp.volumes() == [40]
    assert device._volume_target is None and not device._unconfirmed


@pytest.mark.unit
async def test_losing_the_amplifier_ends_the_wait_for_an_echo(device, amp, monkeypatch):
    monkeypatch.setattr(device, "_ECHO_TIMEOUT_SEC", 5.0)
    monkeypatch.setattr(device, "_discovery_worker", AsyncMock())
    amp.release.set()
    await device.set_volume(40)
    await _settle()
    assert list(device._unconfirmed) == [40]

    await device.rediscover_device()
    await asyncio.wait_for(device._volume_sender, 0.5)

    assert not device._unconfirmed
    assert not any("getStatus" in url for url in amp.urls)
