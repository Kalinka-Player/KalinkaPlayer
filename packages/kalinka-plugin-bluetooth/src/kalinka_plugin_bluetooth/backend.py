"""BlueALSA PCM descriptors and BlueZ metadata/control, outside the audio core.

Supports the 4.3.1 appliance API and the newer Rate/byte-array Volume API.
Only the A2DP sink's PCM *source* is opened; SCO and Bluetooth outputs are not
inputs. The shared adapter and BlueZ daemon are never powered down here.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import AsyncIterator

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from kalinka_plugin_sdk.live_audio import PcmFormat

PROPERTIES = "org.freedesktop.DBus.Properties"
OBJECTS = "org.freedesktop.DBus.ObjectManager"
PCM = "org.bluealsa.PCM1"
BLUEZ = "org.bluez"
DEVICE = "org.bluez.Device1"
PLAYER = "org.bluez.MediaPlayer1"
AUDIO_UUIDS = frozenset(
    f"0000{value}-0000-1000-8000-00805f9b34fb"
    for value in ("110a", "110b", "110c", "110e", "110f")
)


def plain(value):
    if isinstance(value, Variant):
        return plain(value.value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def pcm_format(props: dict) -> PcmFormat:
    code = int(props["Format"])
    layouts = {0x8210: "S16_LE", 0x8318: "S24_3LE", 0x8418: "S24_LE", 0x8420: "S32_LE"}
    if code not in layouts:
        raise ValueError(f"BlueALSA PCM layout 0x{code:04x} is unsupported")
    return PcmFormat(
        int(props.get("Rate", props.get("Sampling", 0))),
        int(props["Channels"]),
        layouts[code],
    )


def volume_percent(value) -> int:
    channels = [value >> 8, value & 255] if isinstance(value, int) else list(value)
    levels = [0 if channel & 0x80 else channel & 0x7F for channel in channels]
    return round(max(levels, default=0) * 100 / 127)


@dataclass(frozen=True)
class Endpoint:
    path: str
    device: str
    address: str
    alias: str
    format: PcmFormat
    codec: str
    running: bool
    sequence: int


class PcmPipe:
    """Nonblocking FD reads; cancellation always removes the loop registration."""

    def __init__(self, fd: int, control: int, block_bytes: int):
        self.fd, self.control = fd, control
        self.block_bytes = block_bytes
        os.set_blocking(fd, False)

    async def read(self) -> bytes:
        loop = asyncio.get_running_loop()
        while self.fd >= 0:
            try:
                return os.read(self.fd, self.block_bytes)
            except BlockingIOError:
                ready = loop.create_future()
                fd = self.fd

                def wake():
                    if not ready.done():
                        ready.set_result(None)

                loop.add_reader(fd, wake)
                try:
                    await ready
                finally:
                    loop.remove_reader(fd)
        return b""

    def close(self) -> None:
        for name in ("fd", "control"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)


class BlueAlsaBackend:
    def __init__(self, adapter: str = "hci0", service: str = "org.bluealsa"):
        self.adapter, self.service = adapter, service
        self.bus: MessageBus | None = None
        self.bluealsa: dict = {}
        self.bluez: dict = {}
        self.changed = asyncio.Event()
        self.owner = ""
        self._restarted = False
        self.agent_handler = None

    async def call(self, destination, path, interface, member, signature="", body=None):
        reply = await asyncio.wait_for(
            self.bus.call(
                Message(
                    destination=destination,
                    path=path,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body or [],
                )
            ),
            5,
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(
                f"{member}: {reply.body[0] if reply.body else reply.error_name}"
            )
        return reply

    async def connect(self):
        self.bus = await asyncio.wait_for(
            MessageBus(
                bus_type=BusType.SYSTEM,
                negotiate_unix_fd=True,
            ).connect(),
            5,
        )
        self.owner = (
            await self.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                "s",
                [BLUEZ],
            )
        ).body[0]
        self.bus.add_message_handler(self._message)
        for rule in (
            f"type='signal',sender='{self.service}'",
            "type='signal',sender='org.bluez'",
            "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged'",
        ):
            await self.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "AddMatch",
                "s",
                [rule],
            )
        await self.refresh()

    def _message(self, message):
        if (
            self.agent_handler is not None
            and message.message_type == MessageType.METHOD_CALL
        ):
            result = self.agent_handler(message)
            if result is not None:
                return result
        if message.message_type == MessageType.SIGNAL:
            if message.member == "NameOwnerChanged":
                if message.body[0] not in (BLUEZ, self.service):
                    return
                self._restarted = True
            self.changed.set()

    async def refresh(self):
        replies = await asyncio.gather(
            self.call(self.service, "/org/bluealsa", OBJECTS, "GetManagedObjects"),
            self.call(BLUEZ, "/", OBJECTS, "GetManagedObjects"),
        )
        self.bluealsa, self.bluez = (plain(reply.body[0]) for reply in replies)

    def endpoints(self) -> list[Endpoint]:
        result = []
        for path, interfaces in self.bluealsa.items():
            props = interfaces.get(PCM, {})
            device = props.get("Device", "")
            if (
                props.get("Transport") != "A2DP-sink"
                or props.get("Mode") != "source"
                or not device.startswith(f"/org/bluez/{self.adapter}/")
            ):
                continue
            peer = self.bluez.get(device, {}).get(DEVICE, {})
            if not peer.get("Connected") or not peer.get("Paired"):
                continue
            result.append(
                Endpoint(
                    path,
                    device,
                    peer.get("Address", "").upper(),
                    peer.get("Alias", "Bluetooth phone"),
                    pcm_format(props),
                    props.get("Codec", "SBC"),
                    bool(props.get("Running")),
                    int(props.get("Sequence", 0)),
                )
            )
        return sorted(
            result, key=lambda item: (item.running, item.sequence), reverse=True
        )

    async def events(self) -> AsyncIterator[None]:
        disconnected = asyncio.create_task(self.bus.wait_for_disconnect())
        try:
            while self.bus and self.bus.connected:
                self.changed.clear()
                if self._restarted:
                    raise RuntimeError("Bluetooth service restarted")
                await self.refresh()
                yield None
                changed = asyncio.create_task(self.changed.wait())
                try:
                    done, _ = await asyncio.wait(
                        (changed, disconnected), return_when=asyncio.FIRST_COMPLETED
                    )
                    if disconnected in done:
                        await disconnected
                        return
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

    async def set_property(self, destination, path, interface, name, signature, value):
        await self.call(
            destination,
            path,
            PROPERTIES,
            "Set",
            "ssv",
            [interface, name, Variant(signature, value)],
        )

    async def open(self, endpoint: Endpoint) -> PcmPipe:
        # One gain authority: the selected Kalinka output. This receiver does
        # not attenuate the PCM a second time.
        await self.set_property(
            self.service, endpoint.path, PCM, "SoftVolume", "b", False
        )
        reply = await self.call(self.service, endpoint.path, PCM, "Open")
        descriptors = list(reply.unix_fds)
        try:
            if len(reply.body) != 2:
                raise RuntimeError(
                    "BlueALSA did not return PCM and control descriptors"
                )
            fd, control = (descriptors[index] for index in reply.body)
            if fd == control:
                raise RuntimeError("BlueALSA returned duplicate descriptors")
            pipe = PcmPipe(
                fd,
                control,
                max(1, endpoint.format.sample_rate // 100)
                * endpoint.format.frame_bytes,
            )
            descriptors.remove(fd)
            descriptors.remove(control)
            return pipe
        finally:
            for fd in descriptors:
                os.close(fd)

    def player(self, endpoint: Endpoint) -> tuple[str, dict] | None:
        for path, interfaces in self.bluez.items():
            if path.startswith(endpoint.device + "/") and PLAYER in interfaces:
                return path, interfaces[PLAYER]
        return None

    def metadata(self, endpoint: Endpoint) -> dict:
        player = self.player(endpoint)
        return player[1].get("Track", {}) if player else {}

    async def command(self, endpoint: Endpoint, method: str):
        player = self.player(endpoint)
        if player is not None:
            await self.call(BLUEZ, player[0], PLAYER, method)
        else:
            await self.call(BLUEZ, endpoint.device, "org.bluez.MediaControl1", method)

    async def set_volume(self, endpoint: Endpoint, percent: int):
        value = round(max(0, min(100, percent)) * 127 / 100)
        old = self.bluealsa.get(endpoint.path, {}).get(PCM, {}).get("Volume")
        if old is None or volume_percent(old) == round(value * 100 / 127):
            return
        signature, volume = (
            ("q", (value << 8) | value)
            if isinstance(old, int)
            else ("ay", bytes([value] * endpoint.format.channels))
        )
        await self.set_property(
            self.service, endpoint.path, PCM, "Volume", signature, volume
        )

    def close(self):
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None
