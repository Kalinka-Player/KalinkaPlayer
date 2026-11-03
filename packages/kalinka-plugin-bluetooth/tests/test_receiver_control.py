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
    ForgetDevice,
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
        if member == "RemoveDevice":
            target = body[0]
            self.bluez = {
                p: i
                for p, i in self.bluez.items()
                if p != target and not p.startswith(target + "/")
            }
            self.bluealsa = {
                p: i for p, i in self.bluealsa.items() if i[PCM]["Device"] != target
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
    await asyncio.gather(*list(plugin.pairing._tasks))


def request(
    plugin,
    member="RequestAuthorization",
    signature="o",
    args=None,
    device=DEVICE_PATH,
    sender=None,
):
    return Message(
        path=AGENT,
        interface="org.bluez.Agent1",
        member=member,
        sender=sender or plugin.backend.owner,
        serial=23,
        signature=signature,
        body=[device, *(args or [])],
    )


async def test_pair_window_authorizes_just_works_without_app_confirmation(plugin):
    from dbus_fast import MessageType

    assert plugin.pairing._agent(request(plugin)).message_type == MessageType.ERROR
    await plugin.execute(OpenPairing(command="open_pairing"))
    assert (
        plugin.pairing._agent(request(plugin, sender=":1.999")).message_type
        == MessageType.ERROR
    )
    await authorize(plugin)
    assert (
        plugin.backend.bus.send.call_args.args[0].message_type
        == MessageType.METHOD_RETURN
    )
    snapshot = await plugin.snapshot()
    assert snapshot.pairing_mode == "just_works" and snapshot.pairing_request is None
    assert not {a.command for a in snapshot.actions} & {
        "answer_pairing",
        "grant_audio",
        "revoke_audio",
    }
    assert any(
        c[1:] == ("RegisterAgent", [AGENT, "NoInputNoOutput"])
        for c in plugin.backend.calls
    )
    assert plugin.access.records[ADDRESS].state == "pending"


@pytest.mark.parametrize(
    "uuid",
    ["0000110d-0000-1000-8000-00805f9b34fb", "0000110D-0000-1000-8000-00805F9B34FB"],
)
async def test_a2dp_service_authorization_completes_pairing_and_allows_reconnect(
    plugin, uuid
):
    from dbus_fast import MessageType

    # BlueZ authorizes incoming AVDTP using the Advanced Audio profile UUID,
    # not the Audio Source or Sink role. An already bonded phone may reach
    # this callback without RequestAuthorization (e.g. after a failed attempt).
    message = request(plugin, "AuthorizeService", "os", [uuid])
    assert plugin.pairing._agent(message).message_type == MessageType.ERROR
    await plugin.execute(OpenPairing(command="open_pairing"))
    assert plugin.pairing._agent(message) is True
    await asyncio.gather(*list(plugin.pairing._tasks))
    assert (
        plugin.backend.bus.send.call_args.args[0].message_type
        == MessageType.METHOD_RETURN
    )
    assert plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    assert plugin.access.records[ADDRESS].state == "pending"

    plugin.backend.transport()
    await plugin._reconcile(plugin.backend)
    assert ADDRESS in plugin.access.devices
    assert not plugin.pairing.active
    assert plugin.backend.audio_devices() == {DEVICE_PATH}

    # The completed grant survives both the closed window and reconnect.
    plugin.backend.bluealsa.clear()
    await plugin._reconcile(plugin.backend)
    plugin.backend.transport(sequence=2)
    await plugin._reconcile(plugin.backend)
    assert plugin.backend.audio_devices() == {DEVICE_PATH}
    assert not any(call[1] == "DisconnectProfile" for call in plugin.backend.calls)


@pytest.mark.parametrize(
    "member,signature,args",
    [
        ("RequestConfirmation", "ou", [42]),
        ("RequestPinCode", "o", []),
        ("RequestPasskey", "o", []),
        ("DisplayPasskey", "ouq", [7, 0]),
        ("DisplayPinCode", "os", ["1234"]),
        ("AuthorizeService", "os", ["unrelated-service"]),
        ("AuthorizeService", "os", ["00001105-0000-1000-8000-00805f9b34fb"]),
        ("AuthorizeService", "os", ["00001116-0000-1000-8000-00805f9b34fb"]),
    ],
)
async def test_just_works_never_invents_or_accepts_comparison_codes(
    plugin, member, signature, args
):
    from dbus_fast import MessageType

    await plugin.execute(OpenPairing(command="open_pairing"))
    result = plugin.pairing._agent(request(plugin, member, signature, args))
    assert result.message_type == MessageType.ERROR
    assert not plugin.access.records


async def test_window_accepts_one_new_device_and_cancel_restores_trust(plugin):
    from dbus_fast import MessageType
    import copy

    other = DEVICE_PATH + "_other"
    props = copy.deepcopy(plugin.backend.bluez[DEVICE_PATH])
    props[DEVICE]["Address"] = "11:22:33:44:55:66"
    plugin.backend.bluez[other] = props
    await plugin.execute(OpenPairing(command="open_pairing"))
    await authorize(plugin)
    assert plugin.pairing._agent(request(plugin, device=other)) is True
    await asyncio.gather(*list(plugin.pairing._tasks))
    assert plugin.backend.bus.send.call_args.args[0].message_type == MessageType.ERROR
    assert set(plugin.pairing.candidates) == {ADDRESS}
    assert not props[DEVICE]["Trusted"]
    plugin.pairing._agent(
        Message(
            path=AGENT,
            interface="org.bluez.Agent1",
            member="Cancel",
            sender=plugin.backend.owner,
            serial=24,
        )
    )
    await plugin._reconcile(plugin.backend)
    assert (
        not plugin.access.records
        and not plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"]
    )


async def test_new_bond_can_reconnect_without_needing_audio_during_pairing(plugin):
    plugin.backend.bluez[DEVICE_PATH][DEVICE]["Paired"] = False
    await plugin.execute(OpenPairing(command="open_pairing"))
    await authorize(plugin)
    assert not plugin.access.devices
    plugin.backend.bluez[DEVICE_PATH][DEVICE]["Paired"] = True
    await plugin._reconcile(plugin.backend)
    assert ADDRESS in plugin.access.devices and not plugin.pairing.active
    plugin.backend.transport()
    await plugin._reconcile(plugin.backend)
    assert plugin.backend.audio_devices() == {DEVICE_PATH}


async def test_existing_provisioning_bond_can_join_audio_through_pair_window(plugin):
    plugin.backend.bluez[DEVICE_PATH][DEVICE]["Trusted"] = True
    await plugin.execute(OpenPairing(command="open_pairing"))
    plugin.backend.transport()
    await plugin._reconcile(plugin.backend)
    assert ADDRESS in plugin.access.devices and not plugin.pairing.active
    assert not plugin.backend.bus.send.called


async def test_window_does_not_adopt_an_existing_ungranted_transport(plugin):
    plugin.backend.transport()
    await plugin.execute(OpenPairing(command="open_pairing"))
    await plugin._reconcile(plugin.backend)
    assert not plugin.access.devices and not plugin.backend.audio_devices()


async def test_forget_removes_bond_and_grant_and_rejects_stale_peer_ids(plugin):
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    plugin.backend.transport()
    old_props = plugin.backend.bluez[DEVICE_PATH]
    await plugin.execute(ForgetDevice(command="forget_device", peer_id=peer.peer_id))
    assert not plugin.access.records and ADDRESS not in plugin.backend.devices()
    assert not plugin.backend.audio_devices() and not (await plugin.snapshot()).peers
    assert ("/org/bluez/hci0", "RemoveDevice", [DEVICE_PATH]) in plugin.backend.calls
    plugin.backend.bluez[DEVICE_PATH] = old_props
    replacement = (await plugin.snapshot()).peers[0]
    assert replacement.peer_id != peer.peer_id
    with pytest.raises(ReceiverError, match="Refresh"):
        await plugin.execute(
            ForgetDevice(command="forget_device", peer_id=peer.peer_id)
        )
    assert ADDRESS in plugin.backend.devices()


async def test_forget_respects_provisioning_lease(plugin, tmp_path):
    peer = (await plugin.snapshot()).peers[0]
    other = PairingWindow(plugin.backend, plugin.access, tmp_path)
    await other.start()
    try:
        with pytest.raises(ReceiverError, match="setup is using"):
            await plugin.execute(
                ForgetDevice(command="forget_device", peer_id=peer.peer_id)
            )
        assert ADDRESS in plugin.backend.devices() and not plugin.access.records
    finally:
        await other.close()


async def test_failed_forget_is_denied_but_not_destructively_replayed(plugin):
    peer = (await plugin.snapshot()).peers[0]
    await plugin.execute(GrantAudio(command="grant_audio", peer_id=peer.peer_id))
    remove = plugin.backend.remove_device
    plugin.backend.remove_device = AsyncMock(
        side_effect=ReceiverError("backend_timeout", "Not removed")
    )
    with pytest.raises(ReceiverError, match="Not removed"):
        await plugin.execute(
            ForgetDevice(command="forget_device", peer_id=peer.peer_id)
        )
    assert not plugin.access.devices
    recovered = AudioAccess(plugin.access.path)
    await recovered.load()
    assert recovered.records[ADDRESS].state == "forgetting"
    await plugin._reconcile(plugin.backend)
    assert plugin.backend.remove_device.await_count == 1
    plugin.backend.remove_device = remove
    await plugin.execute(ForgetDevice(command="forget_device", peer_id=peer.peer_id))
    assert not plugin.access.records and not plugin.backend.devices()


async def test_external_repair_invalidates_forget_handle(plugin):
    from kalinka_plugin_bluetooth.backend import OBJECTS

    peer = (await plugin.snapshot()).peers[0]
    plugin.backend._message(
        Message(
            path="/",
            interface=OBJECTS,
            member="InterfacesRemoved",
            message_type=__import__("dbus_fast").MessageType.SIGNAL,
            signature="oas",
            body=[DEVICE_PATH, [DEVICE]],
        )
    )
    with pytest.raises(ReceiverError, match="pairing has changed"):
        await plugin.execute(
            ForgetDevice(command="forget_device", peer_id=peer.peer_id)
        )
    assert ADDRESS in plugin.backend.devices()


@pytest.mark.parametrize("during", ["refresh", "persistence"])
async def test_repair_during_forget_does_not_remove_replacement(plugin, during):
    peer = (await plugin.snapshot()).peers[0]
    if during == "refresh":
        refresh = plugin.backend.refresh
        calls = 0

        async def changed_refresh():
            nonlocal calls
            await refresh()
            calls += 1
            if calls == 2:
                plugin.backend._device_incarnations[DEVICE_PATH] = 1

        plugin.backend.refresh = changed_refresh
    else:
        persist = plugin.access.forget_bond

        async def changed_persist(*args):
            await persist(*args)
            plugin.backend._device_incarnations[DEVICE_PATH] = 1

        plugin.access.forget_bond = changed_persist
    with pytest.raises(ReceiverError, match="pairing has changed"):
        await plugin.execute(
            ForgetDevice(command="forget_device", peer_id=peer.peer_id)
        )
    assert ADDRESS in plugin.backend.devices()
    assert not any(member == "RemoveDevice" for _, member, _ in plugin.backend.calls)


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
