"""Real D-Bus FD passing on a private bus; never touches host Bluetooth."""

import asyncio
import os
import shutil
import socket

import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from kalinka_plugin_bluetooth import backend


@pytest.mark.skipif(
    shutil.which("dbus-daemon") is None, reason="needs private dbus-daemon"
)
async def test_bluealsa_snapshot_open_and_pcm_fd_ownership(monkeypatch):
    daemon = await asyncio.create_subprocess_exec(
        "dbus-daemon",
        "--session",
        "--nofork",
        "--print-address=1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    service = client = None
    read, write = os.pipe()
    control, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    pcm = "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink"
    soft_volume = []
    try:
        address = (await daemon.stdout.readline()).decode().strip()
        service = await MessageBus(
            bus_address=address, negotiate_unix_fd=True
        ).connect()
        await service.request_name("org.bluez")
        await service.request_name("org.bluealsa")

        def reply(message):
            if message.message_type != MessageType.METHOD_CALL:
                return
            if message.member == "GetManagedObjects":
                objects = (
                    {
                        pcm: {
                            backend.PCM: {
                                "Device": Variant("o", device),
                                "Transport": Variant("s", "A2DP-sink"),
                                "Mode": Variant("s", "source"),
                                "Format": Variant("q", 0x8318),
                                "Channels": Variant("y", 2),
                                "Sampling": Variant("u", 48000),
                                "Running": Variant("b", True),
                                "Codec": Variant("s", "SBC"),
                            }
                        }
                    }
                    if message.destination == "org.bluealsa"
                    else {
                        device: {
                            backend.DEVICE: {
                                "Paired": Variant("b", True),
                                "Connected": Variant("b", True),
                                "Address": Variant("s", "AA:BB:CC:DD:EE:FF"),
                                "Alias": Variant("s", "Phone"),
                            }
                        }
                    }
                )
                return Message.new_method_return(message, "a{oa{sa{sv}}}", [objects])
            if message.member == "Set":
                soft_volume.append(message.body)
                return Message.new_method_return(message)
            if message.member == "Open":
                return Message.new_method_return(
                    message, "hh", [0, 1], unix_fds=[read, control.fileno()]
                )

        service.add_message_handler(reply)
        monkeypatch.setattr(
            backend,
            "MessageBus",
            lambda **kw: MessageBus(
                bus_address=address, negotiate_unix_fd=kw["negotiate_unix_fd"]
            ),
        )
        client = backend.BlueAlsaBackend()
        await client.connect()
        (endpoint,) = client.endpoints()
        assert endpoint.alias == "Phone" and endpoint.format.sample_format == "S24_3LE"
        pipe = await client.open(endpoint)
        assert soft_volume[0][:2] == [backend.PCM, "SoftVolume"]
        assert soft_volume[0][2].value is False
        os.write(write, b"123456abcdef")
        assert await asyncio.wait_for(pipe.read(), 1) == b"123456abcdef"
        transferred = pipe.fd
        pipe.close()
        with pytest.raises(OSError):
            os.fstat(transferred)
        os.fstat(read)  # Closing the client copy did not close the daemon's FD.
    finally:
        if client:
            client.close()
        if service:
            service.disconnect()
            await service.wait_for_disconnect()
        os.close(read)
        os.close(write)
        control.close()
        other.close()
        daemon.terminate()
        await daemon.wait()
