import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kalinka_plugin_sdk.direct_playback import (
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_sdk.live_audio import PcmFormat
from kalinka_plugin_bluetooth.backend import (
    BLUEZ,
    PLAYER,
    BlueAlsaBackend,
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
        self.paused = False
        self.resumes = 0
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
        self.paused = True

    async def resume(self):
        self.paused = False
        self.resumes += 1

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

    def endpoints(self):
        return []

    def audio_devices(self):
        return set()

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


@pytest.mark.parametrize("status", ["stopped", "error"])
async def test_inactive_sender_metadata_is_cleared_without_losing_the_stream(
    service, endpoint, status
):
    backend = BlueAlsaBackend()
    player_path = endpoint.device + "/player0"
    player = {
        "Status": "playing",
        "Track": {"Title": "Yesterday", "Artist": "Old artist", "Duration": 269000},
    }
    backend.bluez = {player_path: {PLAYER: player}}
    await service.update_metadata(backend.metadata(endpoint))
    service.audio(b"123456")
    await settle(service)
    hold, source = service.hold, service.source
    assert hold.plays[0][1].title == "Yesterday"

    # Android may keep Track unchanged after its media session has stopped.
    player["Status"] = status
    await service.update_metadata(backend.metadata(endpoint))
    track, position = hold.shown[-1]
    assert track.title == "Bluetooth audio"
    assert track.performer is None and track.duration == 0
    assert track.album.title == endpoint.alias and position == 0
    assert service.hold is hold and service.source is source
    assert len(hold.plays) == 1 and not source.closed

    # Filtering metadata must not prevent Play from reaching the stopped player.
    backend.call = AsyncMock()
    await backend.command(endpoint, "Play")
    backend.call.assert_awaited_once_with(BLUEZ, player_path, PLAYER, "Play")
    player.update(Status="playing", Track={"Title": "Today"})
    await service.update_metadata(backend.metadata(endpoint))
    assert hold.shown[-1][0].title == "Today"


@pytest.mark.parametrize(
    "status", ["playing", "paused", "forward-seek", "reverse-seek", None]
)
def test_sender_metadata_is_kept_while_active_or_paused(endpoint, status):
    backend = BlueAlsaBackend()
    player = {"Track": {"Title": "Song"}}
    if status is not None:
        player["Status"] = status
    backend.bluez = {endpoint.device + "/player0": {PLAYER: player}}
    assert backend.metadata(endpoint) == {"Title": "Song"}


@pytest.mark.parametrize(
    "running,status", [(False, None), (True, "paused"), (True, "stopped")]
)
async def test_idle_sender_pauses_output_and_pcm_resumes_without_remote_play(
    service, endpoint, running, status
):
    service.audio(b"123456")
    await settle(service)
    hold, source = service.hold, service.source
    await service.connected(replace(endpoint, running=running))
    service.last_audio -= 2
    await service.update_activity(status)
    assert hold.paused and service.input_idle and not service.paused
    assert "paused" in service.status
    assert source.resets == 1 and source.data == b""
    await service.update_activity(status)
    assert source.resets == 1

    # System/browser audio can resume without a fresh AVRCP or Running event.
    service.backend.changed.clear()
    service.audio(b"abcdef")
    assert service.backend.changed.is_set()
    await service.update_activity(status)
    assert not hold.paused and not service.input_idle and hold.resumes == 1
    assert source.data == b"abcdef" and service.hold is hold
    assert service.backend.commands == [] and len(service.direct.holds) == 1


async def test_short_sender_gap_does_not_restart_output(service, endpoint):
    service.audio(b"123456")
    await settle(service)
    await service.connected(replace(endpoint, running=False))
    await service.update_activity("paused")
    assert not service.hold.paused and service.source.resets == 0
    await service.connected(endpoint)
    await service.update_activity("playing")
    assert service.hold.resumes == 0


async def test_expected_audio_stall_keeps_renderer_state(service):
    service.audio(b"123456")
    await settle(service)
    service.last_audio -= 2
    await service.update_activity("playing")
    assert not service.hold.paused and service.source.resets == 0
    assert "paused" not in service.status
    service.output_error = "Renderer unavailable"
    await service.update_activity("stopped")
    assert not service.hold.paused and service.output_error == "Renderer unavailable"


async def test_idle_monitor_cannot_undo_a_kalinka_pause(service):
    service.audio(b"123456")
    await settle(service)
    await service.command(TransportRequest(TransportKind.PAUSE))
    service.audio(b"abcdef")
    await service.update_activity("playing")
    assert service.hold.paused and service.hold.resumes == 0
    assert service.backend.commands == ["Pause"]


async def test_queue_takeover_while_idle_cannot_be_undone_by_pcm(service, endpoint):
    service.audio(b"123456")
    await settle(service)
    await service.connected(replace(endpoint, running=False))
    service.last_audio -= 2
    await service.update_activity("paused")
    hold = service.hold
    hold.listener.on_revoked(RevokeReason.QUEUE_PLAY)
    service.audio(b"abcdef")
    await service.update_activity("playing")
    await settle(service)
    assert service.blocked and service.hold is None and not service.input_idle
    assert service.source.data == b"" and hold.resumes == 0
    assert len(service.direct.holds) == 1


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


async def test_sender_play_resumes_the_hold_paused_through_kalinka(service, endpoint):
    service.audio(b"123456")
    await settle(service)
    hold = service.hold
    await service.command(TransportRequest(TransportKind.PAUSE))
    await service.connected(replace(endpoint, running=False))
    assert hold.paused

    await service.connected(endpoint)
    service.audio(b"abcdef")
    await settle(service)
    assert not hold.paused and hold.resumes == 1
    assert not service.paused and service.hold is hold
    assert service.source.data == b"abcdef"
    assert len(service.direct.holds) == 1
    # The sender already started: do not echo Play or restart on another update.
    assert service.backend.commands == ["Pause"]
    await service.connected(endpoint)
    assert hold.resumes == 1


async def test_sender_running_transition_does_not_restart_an_unpaused_hold(
    service, endpoint
):
    service.audio(b"123456")
    await settle(service)
    await service.connected(replace(endpoint, running=False))
    await service.connected(endpoint)
    assert service.hold.resumes == 0
    assert service.source.resets == 0


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
