import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kalinka_plugin_sdk.direct_playback import (
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_sdk.live_audio import PcmFormat
from kalinka_plugin_bluetooth.backend import (
    Endpoint,
    PcmPipe,
    pcm_format,
    volume_percent,
)
from kalinka_plugin_bluetooth.pairing import AudioAccess, PairingWindow
from kalinka_plugin_bluetooth.service import BluetoothService


class Source:
    def __init__(self, format):
        self.format, self.data, self.closed, self.resets = format, bytearray(), False, 0

    def offer(self, pcm):
        self.data.extend(pcm)

    def reset(self, format=None):
        self.resets += 1
        self.data.clear()
        self.format = format or self.format

    def close(self):
        self.closed = True


class Live:
    def create_pcm(self, format):
        return Source(format)


class Hold:
    def __init__(self, listener):
        self.listener, self.active, self.plays, self.shown, self.volumes = (
            listener,
            True,
            [],
            [],
            [],
        )

    async def play_live(self, source, track, **kwargs):
        self.plays.append((source, track))

    async def show(self, track, position):
        self.shown.append((track, position))

    async def release(self):
        self.active = False

    async def pause(self):
        pass

    async def resume(self):
        pass

    async def set_volume(self, value):
        self.volumes.append(value)


class Direct:
    def __init__(self):
        self.holds = []

    async def acquire(self, title, listener, *, seekable):
        assert title == "Bluetooth" and not seekable
        hold = Hold(listener)
        self.holds.append(hold)
        return hold


class Backend:
    adapter = "hci0"

    def __init__(self):
        self.commands, self.volumes, self.calls = [], [], []
        self.changed = asyncio.Event()
        self.agent_handler = None

    async def command(self, endpoint, method):
        self.commands.append(method)

    async def set_volume(self, endpoint, value):
        self.volumes.append(value)

    async def call(self, *args):
        self.calls.append(args)
        return SimpleNamespace(
            body=[
                {
                    "Powered": True,
                    "Pairable": False,
                    "Discoverable": False,
                    "DiscoverableTimeout": 0,
                }
            ]
        )

    async def set_property(self, *args):
        self.calls.append(args)


@pytest.fixture
def endpoint():
    return Endpoint(
        "/pcm",
        "/org/bluez/hci0/dev_00_11_22_33_44_55",
        "00:11:22:33:44:55",
        "Phone",
        PcmFormat(48000, 2, "S24_3LE"),
        "SBC",
        True,
        1,
    )


@pytest.fixture
async def service(endpoint):
    service = BluetoothService(Live(), Direct(), Backend())
    await service.connected(endpoint)
    yield service
    await service.close()


async def settle(service):
    for _ in range(5):
        await asyncio.sleep(0)


async def test_pcm_is_preserved_and_claims_once_even_with_fragmented_frames(service):
    service.audio(b"abc")
    service.audio(b"defg")
    service.audio(b"hijkl")
    await settle(service)
    assert service.source.data == b"abcdefghijkl"
    assert len(service.direct.holds) == 1
    source, track = service.hold.plays[0]
    assert source is service.source and track.title == "Bluetooth audio"
    await service.update_metadata(
        {"Title": "Song", "Artist": "Artist", "Duration": 10001}
    )
    assert service.hold.shown[0][0].performer.name == "Artist"
    assert service.hold.shown[0][0].duration == 10
    assert len(service.hold.plays) == 1


async def test_takeover_does_not_reclaim_until_a_new_sender_play_transition(
    service, endpoint
):
    service.audio(b"123456")
    await settle(service)
    service.hold.listener.on_revoked(RevokeReason.QUEUE_PLAY)
    service.audio(b"abcdef")
    await settle(service)
    assert service.blocked and service.hold is None
    assert service.source.data == b"" and service.backend.commands == ["Pause"]
    assert len(service.direct.holds) == 1
    await service.connected(replace(endpoint, running=False))
    await service.connected(endpoint)
    service.last_claim = 0
    service.audio(b"uvwxyz")
    await settle(service)
    assert not service.blocked and len(service.direct.holds) == 2


async def test_old_listener_cannot_revoke_a_new_phone(service, endpoint):
    service.audio(b"123456")
    await settle(service)
    old = service.hold
    old_source = service.source
    await service.connected(
        replace(endpoint, path="/second", address="AA:BB:CC:DD:EE:FF")
    )
    old.listener.on_revoked(RevokeReason.OTHER_HOLDER)
    assert old_source.closed and not old.active
    assert not service.blocked


async def test_pause_discards_audio_and_volume_echo_does_not_loop(service):
    service.audio(b"123456")
    await settle(service)
    await service.command(TransportRequest(TransportKind.PAUSE))
    service.audio(b"abcdef")
    assert service.source.data == b"123456"
    await service.command(TransportRequest(TransportKind.RESUME))
    assert service.source.resets == 1
    await service.output_volume(42)
    await service.input_volume(42)
    assert service.backend.volumes == [42] and service.hold.volumes == []
    await service.input_volume(25)
    assert service.hold.volumes == [25]


def test_backend_normalizes_api_versions_and_rejects_unknown_layouts():
    assert pcm_format(
        {"Sampling": 44100, "Channels": 2, "Format": 0x8210}
    ) == PcmFormat(44100)
    assert (
        pcm_format({"Rate": 48000, "Channels": 2, "Format": 0x8418}).sample_format
        == "S24_LE"
    )
    assert volume_percent(0x7F7F) == volume_percent([127, 127]) == 100
    assert volume_percent(0xFFFF) == 0
    with pytest.raises(ValueError):
        pcm_format({"Rate": 48000, "Channels": 2, "Format": 0x4210})


async def test_pipe_cancellation_and_eof_release_real_descriptors():
    read, write = os.pipe()
    control, other = os.pipe()
    pipe = PcmPipe(read, control, 6)
    pending = asyncio.create_task(pipe.read())
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    os.write(write, b"123456")
    assert await pipe.read() == b"123456"
    os.close(write)
    assert await pipe.read() == b""
    pipe.close()
    pipe.close()
    os.close(other)
    with pytest.raises(OSError):
        os.fstat(read)


async def test_audio_access_is_separate_and_pairing_windows_share_a_lease(tmp_path):
    access = AudioAccess(tmp_path / "access.json")
    await access.load()
    await access.allow("aa:bb:cc:dd:ee:ff", "Phone")
    restored = AudioAccess(access.path)
    await restored.load()
    assert restored.devices == {"AA:BB:CC:DD:EE:FF": "Phone"}
    await access.forget("AA:BB:CC:DD:EE:FF")
    assert not access.devices
    first = PairingWindow(Backend(), access, tmp_path)
    second = PairingWindow(Backend(), access, tmp_path)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="using this adapter"):
            await second.start()
    finally:
        await first.close()
    await second.start()
    await second.close()
    assert (tmp_path / "bluetooth-hci0.lock").exists()
    assert not first.active and not second.active


async def test_queued_control_from_old_phone_does_not_affect_new_phone(
    service, endpoint
):
    service.audio(b"123456")
    await settle(service)
    listener = service.hold.listener
    listener.on_command(TransportRequest(TransportKind.PAUSE))
    await service.connected(
        replace(endpoint, path="/second", address="AA:BB:CC:DD:EE:FF")
    )
    await settle(service)
    assert not service.paused
    assert service.backend.commands == []


async def test_old_hold_cannot_revoke_reacquired_output_for_same_phone(
    service, endpoint
):
    service.audio(b"123456")
    await settle(service)
    old = service.hold.listener
    old.on_revoked(RevokeReason.QUEUE_PLAY)
    await settle(service)
    await service.connected(replace(endpoint, running=False))
    await service.connected(endpoint)
    service.audio(b"abcdef")
    await settle(service)
    new = service.hold
    old.on_revoked(RevokeReason.QUEUE_PLAY)
    assert not service.blocked and service.hold is new


async def test_cancelled_pairing_cleanup_releases_adapter_lease(tmp_path):
    access = AudioAccess(tmp_path / "access.json")
    backend = Backend()
    window = PairingWindow(backend, access, tmp_path)
    await window.start()
    entered = asyncio.Event()
    call = backend.call

    async def blocked_unregister(*args):
        if "UnregisterAgent" in args:
            entered.set()
            await asyncio.Event().wait()
        return await call(*args)

    backend.call = blocked_unregister
    closing = asyncio.create_task(window.close())
    await entered.wait()
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    assert window._lock_fd is None and backend.agent_handler is None
    successor = PairingWindow(Backend(), access, tmp_path)
    await successor.start()
    await successor.close()


async def test_disconnect_closes_source_before_waiting_for_pending_claim(service):
    source = service.source
    ready, cancelling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_claim():
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await finish.wait()

    service.claiming = asyncio.create_task(slow_claim())
    await ready.wait()
    disconnecting = asyncio.create_task(service.disconnected())
    await cancelling.wait()
    assert source.closed and service.source is None
    finish.set()
    await disconnecting


@pytest.mark.parametrize("trust_fails", [False, True])
async def test_authorizing_bonded_phone_trusts_actual_bluez_path(tmp_path, trust_fails):
    from unittest.mock import AsyncMock
    from kalinka_plugin_bluetooth import KalinkaPluginBluetooth
    from kalinka_plugin_bluetooth.backend import BLUEZ, DEVICE

    plugin = KalinkaPluginBluetooth()
    plugin.access = AudioAccess(tmp_path / "access.json")
    address = "AA:BB:CC:DD:EE:FF"
    path = "/org/bluez/hci0/dev_11_22_33_44_55_66"
    setter = AsyncMock(side_effect=RuntimeError("trust failed") if trust_fails else None)
    plugin.backend = SimpleNamespace(
        bluez={path: {DEVICE: {"Address": address, "Alias": "Phone", "Paired": True}}},
        set_property=setter,
    )
    plugin.actions = {"authorize_device": address}
    await plugin._apply_actions()
    setter.assert_awaited_once_with(BLUEZ, path, DEVICE, "Trusted", "b", True)
    if trust_fails:
        assert not plugin.access.devices
        assert plugin.error == "trust failed"
    else:
        assert plugin.access.devices == {address: "Phone"}
        assert plugin.error is None
