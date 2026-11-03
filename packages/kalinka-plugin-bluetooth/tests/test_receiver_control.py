import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbus_fast import Message

from kalinka_plugin_bluetooth import BluetoothConfig, KalinkaPluginBluetooth
from kalinka_plugin_bluetooth.backend import (
    ADAPTER,
    BLUEZ,
    DEVICE,
    PCM,
    BlueAlsaBackend,
)
from kalinka_plugin_bluetooth.pairing import AGENT, AudioAccess, PairingWindow
from kalinka_plugin_sdk.receiver_control import (
    ClosePairing,
    DisconnectAudio,
    GrantAudio,
    OpenPairing,
    ReceiverError,
    RevokeAudio,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
# Object paths need not be derived from the current identity address.
DEVICE_PATH = "/org/bluez/hci0/dev_11_22_33_44_55_66"


class Backend(BlueAlsaBackend):
    def __init__(self, trusted=False):
        super().__init__("hci0", "org.bluealsa")
        self.owner = ":1.50"
        self.bus = SimpleNamespace(send=AsyncMock())
        self.calls = []
        self.bluez = {
            "/org/bluez/hci0": {
                ADAPTER: {
                    "Alias": "Kitchen",
                    "Powered": True,
                    "Pairable": False,
                    "Discoverable": False,
                    "DiscoverableTimeout": 0,
                }
            },
            DEVICE_PATH: {
                DEVICE: {
                    "Address": ADDRESS,
                    "Alias": "Phone",
                    "Paired": True,
                    "Connected": True,
                    "Trusted": trusted,
                }
            },
        }

    async def refresh(self):
        pass

    async def call(self, dest, path, iface, member, signature="", body=None):
        self.calls.append((path, member, body))
        if member == "GetAll":
            return SimpleNamespace(body=[self.bluez[path][body[0]]])
        if member == "DisconnectProfile":
            self.bluealsa = {
                p: i for p, i in self.bluealsa.items() if i[PCM]["Device"] != path
            }
        return SimpleNamespace(body=[])

    async def set_property(self, dest, path, iface, name, signature, value):
        assert dest == BLUEZ
        self.calls.append((path, name, value))
        self.bluez[path][iface][name] = value

    def transport(self, sequence=1, format=0x8210):
        self.bluealsa["/pcm"] = {
            PCM: {
                "Device": DEVICE_PATH,
                "Transport": "A2DP-sink",
                "Mode": "source",
                "Sampling": 48000,
                "Channels": 2,
                "Format": format,
                "Running": False,
                "Sequence": sequence,
            }
        }


@pytest.fixture
async def plugin(tmp_path):
    p = KalinkaPluginBluetooth()
    p.context = SimpleNamespace(
        config=BluetoothConfig(enabled=True),
        server_name=lambda: "Kitchen",
        receiver_changed=lambda: None,
    )
    p.backend = Backend()
    p._connected = True
    p.access = AudioAccess(tmp_path / "access.json")
    p.pairing = PairingWindow(p.backend, p.access, tmp_path)
    yield p
    await p.pairing.close()


@pytest.mark.parametrize("trusted", [False, True])
async def test_grant_restores_prior_trust_and_disconnects_only_audio(plugin, trusted):
    backend = plugin.backend
    backend.bluez[DEVICE_PATH][DEVICE]["Trusted"] = trusted
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    assert plugin.access.records[ADDRESS].trust_before_grant == trusted
    assert backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    # Repeated grant must not replace the baseline with the trust we set.
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    backend.transport()
    await plugin.execute(RevokeAudio(command="revoke_audio", peer_id=peer.peer_id))
    assert ADDRESS not in plugin.access.devices
    assert backend.bluez[DEVICE_PATH][DEVICE]["Trusted"] == trusted
    assert backend.bluez[DEVICE_PATH][DEVICE]["Paired"]
    assert not backend.audio_devices()
    assert (
        DEVICE_PATH,
        "DisconnectProfile",
        ["0000110a-0000-1000-8000-00805f9b34fb"],
    ) in backend.calls
    await plugin.execute(RevokeAudio(command="revoke_audio", peer_id=peer.peer_id))


async def test_pending_trust_write_is_recovered_after_restart(plugin):
    await plugin.access.begin(ADDRESS, "Phone", False)
    plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"] = True
    recovered = AudioAccess(plugin.access.path)
    await recovered.load()
    plugin.access = recovered
    await plugin._reconcile(plugin.backend)
    assert not recovered.devices and not recovered.records
    assert not plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]


async def test_legacy_grant_does_not_invent_prior_trust(plugin):
    plugin.access.path.write_text(json.dumps({ADDRESS: "Phone"}))
    await plugin.access.load()
    plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"] = True
    plugin.backend.transport()
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(RevokeAudio(command="revoke_audio", peer_id=peer.peer_id))
    assert plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    assert not plugin.backend.audio_devices()


async def authorize(plugin):
    message = Message(
        path=AGENT,
        interface="org.bluez.Agent1",
        member="RequestAuthorization",
        sender=plugin.backend.owner,
        serial=1,
        signature="o",
        body=[DEVICE_PATH],
    )
    assert plugin.pairing._agent(message) is True
    for _ in range(10):
        if plugin.pairing.request is not None:
            break
        await asyncio.sleep(0)
    await plugin.pairing.answer(plugin.pairing.request.request_id, True)
    await asyncio.gather(*list(plugin.pairing._tasks))


async def agent_request(
    plugin, member="RequestConfirmation", signature="ou", args=None
):
    await plugin.execute(OpenPairing(command="open_pairing"))
    message = Message(
        path=AGENT,
        interface="org.bluez.Agent1",
        member=member,
        sender=plugin.backend.owner,
        serial=23,
        signature=signature,
        body=[DEVICE_PATH, *(args if args is not None else [42])],
    )
    assert plugin.pairing._agent(message) is True
    for _ in range(10):
        await asyncio.sleep(0)
        if plugin.pairing.request is not None:
            break
    return message, plugin.pairing.request


async def test_numeric_comparison_requires_answer_and_rejects_stale_clients(plugin):
    from dbus_fast import MessageType

    message, request = await agent_request(plugin)
    assert request.code == "000042" and request.device_name == "Phone"
    assert request.method == "confirmation"
    assert not plugin.backend.bus.send.called
    assert not plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    assert not plugin.access.records
    assert (await plugin.snapshot()).pairing_request.code == "000042"
    assert plugin.pairing._agent(message).message_type == MessageType.ERROR
    await plugin.pairing.answer(request.request_id, True)
    with pytest.raises(ReceiverError, match="already ended"):
        await plugin.pairing.answer(request.request_id, False)
    await asyncio.gather(*list(plugin.pairing._tasks))
    assert (
        plugin.backend.bus.send.call_args.args[0].message_type
        == MessageType.METHOD_RETURN
    )
    assert not plugin.access.devices
    assert any(
        call[1:] == ("RegisterAgent", [AGENT, "DisplayYesNo"])
        for call in plugin.backend.calls
    )


@pytest.mark.parametrize("end", ["reject", "cancel", "close", "timeout"])
async def test_unapproved_pairing_never_grants_trust_or_audio(plugin, end):
    from dbus_fast import MessageType
    from time import monotonic

    await plugin.execute(OpenPairing(command="open_pairing"))
    if end == "timeout":
        plugin.pairing.deadline = monotonic() + 0.03
    _, request = await agent_request(plugin)
    tasks = list(plugin.pairing._tasks)
    if end == "reject":
        await plugin.pairing.answer(request.request_id, False)
    elif end == "close":
        await plugin.pairing.close()
    elif end == "cancel":
        plugin.pairing._agent(
            Message(
                path=AGENT,
                interface="org.bluez.Agent1",
                member="Cancel",
                sender=plugin.backend.owner,
                serial=24,
            )
        )
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert plugin.pairing.request is None
    assert not plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    assert plugin.backend.bluez[DEVICE_PATH][DEVICE]["Paired"]
    assert not plugin.access.records
    assert plugin.backend.bus.send.call_args.args[0].message_type == MessageType.ERROR
    with pytest.raises(ReceiverError):
        await plugin.pairing.answer(request.request_id, True)


@pytest.mark.parametrize(
    "member,method,code,signature,body",
    [
        ("RequestAuthorization", "authorization", None, "", []),
        ("RequestPinCode", "pin", "aB12", "s", ["aB12"]),
        ("RequestPasskey", "passkey", "000007", "u", [7]),
    ],
)
async def test_other_callbacks_return_the_users_answer(
    plugin, member, method, code, signature, body
):
    _, request = await agent_request(plugin, member, "o", [])
    assert request.method == method and request.code is None
    if method == "passkey":
        with pytest.raises(ReceiverError):
            await plugin.pairing.answer(request.request_id, True, "7")
    await plugin.pairing.answer(request.request_id, True, code)
    await asyncio.gather(*list(plugin.pairing._tasks))
    reply = plugin.backend.bus.send.call_args.args[0]
    assert reply.signature == signature and reply.body == body


async def test_display_callback_is_not_comparison_and_times_out(plugin):
    _, request = await agent_request(plugin, "DisplayPasskey", "ouq", [7, 2])
    await asyncio.gather(*list(plugin.pairing._tasks))
    assert request.code == "000007" and request.entered == 2
    assert request.method == "display_passkey" and not plugin.access.records
    with pytest.raises(ReceiverError, match="on your device"):
        await plugin.pairing.answer(request.request_id, True)
    plugin.pairing.pending.deadline = 0
    await plugin.pairing.expire_request()
    assert plugin.pairing.request is None
    assert (DEVICE_PATH, "CancelPairing", None) in plugin.backend.calls


async def test_rejected_request_can_be_retried_with_a_fresh_token(plugin):
    _, old = await agent_request(plugin)
    await plugin.pairing.answer(old.request_id, False)
    await asyncio.gather(*list(plugin.pairing._tasks))

    _, new = await agent_request(plugin)
    assert new.request_id != old.request_id
    with pytest.raises(ReceiverError):
        await plugin.pairing.answer(old.request_id, True)
    await plugin.pairing.answer(new.request_id, False)
    await asyncio.gather(*list(plugin.pairing._tasks))


async def test_window_expiry_preserves_established_audio_and_restores_adapter(plugin):
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    plugin.backend.transport()
    await plugin.execute(OpenPairing(command="open_pairing"))
    adapter = plugin.backend.bluez["/org/bluez/hci0"][ADAPTER]
    assert adapter["Discoverable"] and adapter["Pairable"]
    assert adapter["DiscoverableTimeout"] == 120
    await plugin.pairing._expire(0)
    assert not adapter["Discoverable"] and not adapter["Pairable"]
    assert plugin.backend.audio_devices() == {DEVICE_PATH}
    assert ADDRESS in plugin.access.devices
    assert not plugin.pairing.active and plugin.pairing._lock_fd is None


async def test_pairing_commits_on_valid_pcm_endpoint_without_playback(plugin):
    first = await plugin.execute(OpenPairing(command="open_pairing"))
    second = await plugin.execute(OpenPairing(command="open_pairing"))
    assert first.pairing.window_id == second.pairing.window_id
    assert 0 < second.pairing.remaining_seconds <= 120
    await authorize(plugin)
    assert not plugin.access.devices
    assert plugin.access.records[ADDRESS].state == "pending"
    plugin.backend.transport()
    await plugin._reconcile(plugin.backend)
    assert plugin.access.devices == {ADDRESS: "Phone"}
    snapshot = await plugin.snapshot()
    assert snapshot.pairing is None
    assert snapshot.recent_peer_id == snapshot.peers[0].peer_id
    assert snapshot.sessions and not snapshot.sessions[0].playing


async def test_closing_pairing_rolls_back_provisional_access(plugin):
    snapshot = await plugin.execute(OpenPairing(command="open_pairing"))
    await authorize(plugin)
    await plugin.execute(
        ClosePairing(command="close_pairing", window_id=snapshot.pairing.window_id)
    )
    assert not plugin.access.records
    assert not plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    assert plugin.backend.bluez[DEVICE_PATH][DEVICE]["Paired"]
    newer = await plugin.execute(OpenPairing(command="open_pairing"))
    with pytest.raises(ReceiverError, match="already ended"):
        await plugin.execute(
            ClosePairing(command="close_pairing", window_id=snapshot.pairing.window_id)
        )
    assert plugin.pairing.window_id == newer.pairing.window_id


@pytest.mark.parametrize("enabled", [True, False])
async def test_ungranted_audio_is_disconnected_even_if_trusted_and_unplayable(
    plugin, enabled
):
    plugin.context.config.enabled = enabled
    plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"] = True
    plugin.backend.transport(format=0x4210)
    await plugin._reconcile(plugin.backend)
    assert not plugin.backend.audio_devices()
    # Unrelated/shared trust is untouched without an audio grant baseline.
    assert plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]


async def test_old_session_cannot_disconnect_a_replacement(plugin):
    plugin.backend.transport()
    plugin._sync_sessions(plugin.backend.endpoints())
    old = (await plugin.snapshot()).sessions[0].session_id
    plugin.backend.transport(sequence=2)
    with pytest.raises(ReceiverError, match="session has ended"):
        await plugin.execute(
            DisconnectAudio(command="disconnect_audio", session_id=old)
        )
    assert plugin.backend.audio_devices()


async def test_name_inherits_streamer_and_defers_during_provisioning(plugin, tmp_path):
    competing = PairingWindow(plugin.backend, plugin.access, tmp_path)
    await competing.start()
    try:
        plugin.context.server_name = lambda: "Renamed"
        await plugin.server_name_changed()
        assert (await plugin.snapshot()).effective_name == "Kitchen"
        assert plugin._name_pending
        with pytest.raises(ReceiverError, match="using the adapter"):
            await plugin.execute(OpenPairing(command="open_pairing"))
    finally:
        await competing.close()
    await plugin._reconcile(plugin.backend)
    assert (await plugin.snapshot()).effective_name == "Renamed"
    plugin.context.config.device_name = "My Bluetooth"
    await plugin.config_changed({"device_name"})
    plugin.context.server_name = lambda: "Another name"
    await plugin.server_name_changed()
    assert (await plugin.snapshot()).effective_name == "My Bluetooth"


async def test_failed_revoke_remains_denied_and_reconciles_cleanup(plugin):
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    plugin.backend.transport()
    disconnect = plugin.backend.disconnect_audio
    plugin.backend.disconnect_audio = AsyncMock(
        side_effect=ReceiverError("backend_timeout", "Phone did not disconnect")
    )
    with pytest.raises(ReceiverError, match="did not disconnect"):
        await plugin.execute(RevokeAudio(command="revoke_audio", peer_id=peer.peer_id))
    recovered = AudioAccess(plugin.access.path)
    await recovered.load()
    assert not recovered.devices and recovered.records[ADDRESS].state == "revoked"
    assert (await plugin.snapshot()).state == "error"
    plugin.backend.disconnect_audio = disconnect
    await plugin._reconcile(plugin.backend)
    assert not plugin.backend.audio_devices() and not plugin.access.records
    assert (await plugin.snapshot()).state == "ready"


async def test_disabling_stops_pairing_and_disconnects_without_removing_saved_grant(
    plugin,
):
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    await plugin.execute(OpenPairing(command="open_pairing"))
    plugin.backend.transport()
    plugin.context.config.enabled = False
    await plugin.config_changed({"enabled"})
    assert not plugin.pairing.active and not plugin.backend.audio_devices()
    assert plugin.access.devices == {ADDRESS: "Phone"}
    with pytest.raises(ReceiverError, match="Enable Bluetooth"):
        await plugin.execute(OpenPairing(command="open_pairing"))
    plugin.backend.transport(sequence=2)
    await plugin._reconcile(plugin.backend)
    assert not plugin.backend.audio_devices()


async def test_coalesced_reconnect_without_sequence_invalidates_session_handle(plugin):
    from kalinka_plugin_bluetooth.backend import OBJECTS

    plugin.backend.transport(sequence=0)
    plugin._sync_sessions(plugin.backend.endpoints())
    old = (await plugin.snapshot()).sessions[0].session_id
    plugin.backend._message(
        Message.new_signal(
            "/org/bluealsa",
            OBJECTS,
            "InterfacesRemoved",
            "oas",
            ["/pcm", [PCM]],
        )
    )
    # The next object-manager refresh already sees the replacement at the
    # same path, with no Sequence property available to distinguish it.
    plugin.backend.transport(sequence=0)
    with pytest.raises(ReceiverError, match="session has ended"):
        await plugin.execute(
            DisconnectAudio(command="disconnect_audio", session_id=old)
        )
    assert plugin.backend.audio_devices()
