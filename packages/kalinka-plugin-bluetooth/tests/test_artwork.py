"""Cover art is optional, asynchronous and tied to the current sender/track."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dbus_fast import Message, Variant

from kalinka_plugin_bluetooth import KalinkaPluginBluetooth
from kalinka_plugin_bluetooth.artwork import (
    MAX_IMAGE_BYTES,
    ObexClient,
    TRANSFER,
)
from kalinka_plugin_bluetooth.backend import ADAPTER, BlueAlsaBackend, PLAYER
from kalinka_plugin_bluetooth.service import BluetoothService
import test_bluetooth as bluetooth_helpers
from test_bluetooth import Direct, Live, settle

endpoint = bluetooth_helpers.endpoint

JPEG = b"\xff\xd8\xff\xe0cover"
PNG = b"\x89PNG\r\n\x1a\ncover"


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def receiver(endpoint):
    backend = BlueAlsaBackend()
    backend.bluez = {
        "/org/bluez/hci0": {ADAPTER: {"Address": "AA:BB:CC:DD:EE:FF"}},
        endpoint.device
        + "/player0": {
            PLAYER: {
                "Status": "playing",
                "ObexPort": 4097,
                "Track": {"Title": "First"},
            }
        },
    }
    client = SimpleNamespace(
        connect=AsyncMock(),
        check_alive=Mock(),
        fetch=AsyncMock(return_value=(JPEG, "image/jpeg")),
        close=AsyncMock(),
    )
    service = BluetoothService(Live(), Direct(), backend)
    service.artwork.client_factory = lambda: client
    await service.connected(endpoint)
    yield service, backend, client
    await service.close()


async def metadata(service, backend, **track):
    backend.player(service.endpoint)[1]["Track"] = track
    await service.update_metadata(backend.metadata(service.endpoint))


async def connect_artwork(service, backend, client):
    await service.update_metadata(backend.metadata(service.endpoint))
    await until(lambda: client.connect.await_count == 1)
    assert client.fetch.await_count == 0  # Connect before BlueZ has ImgHandle.


async def test_artwork_reaches_now_playing_and_content_without_restarting_audio(
    receiver,
):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)
    client.connect.assert_awaited_once_with(
        service.endpoint.address, 4097, "AA:BB:CC:DD:EE:FF"
    )
    service.audio(b"123456")
    await settle(service)
    hold, source = service.hold, service.source
    backend.player(service.endpoint)[1]["Position"] = 12500
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    await until(lambda: service.artwork_url is not None)
    assert all(position == 12500 for _, position in hold.shown)
    track = hold.shown[-1][0]
    assert track.image == track.album.image
    assert track.image.small == track.image.thumbnail == track.image.large
    assert track.image.large.startswith("/content/bluetooth/art-")
    plugin = KalinkaPluginBluetooth()
    plugin.service = service
    info = await plugin.interface.get_content_info(track.image.large.rsplit("/", 1)[1])
    assert info.mime_type == "image/jpeg" and info.size == len(JPEG)
    assert info.reader().read() == JPEG and info.reader().read() == JPEG
    assert info.local_path is None
    assert await plugin.interface.get_content_info("../../etc/passwd") is None
    assert len(hold.plays) == 1 and service.source is source and not source.closed
    await service.update_metadata(backend.metadata(service.endpoint))
    await settle(service)
    client.fetch.assert_awaited_once_with("0000001")


async def test_late_artwork_cannot_replace_new_track_and_same_handle_is_refetched(
    receiver,
):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)
    pending = asyncio.Event()

    async def slow(handle):
        if client.fetch.await_count == 1:
            await pending.wait()
            return JPEG, "image/jpeg"
        return PNG, "image/png"

    client.fetch.side_effect = slow
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    await until(lambda: client.fetch.await_count == 1)
    await metadata(service, backend, Title="Second", ImgHandle="0000001")
    assert service.track().image is None
    pending.set()
    await until(lambda: service.artwork_url is not None)
    assert client.fetch.await_count == 2
    assert len(service.artwork.images) == 1
    assert (
        service.artwork.content(service.artwork_url.rsplit("/", 1)[1]).mime_type
        == "image/png"
    )
    assert service.track().title == "Second"


async def test_pause_preserves_artwork_stop_clears_it_and_disconnect_cleans_up(
    receiver,
):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    await until(lambda: service.artwork_url is not None)
    image = service.track().image
    player = backend.player(service.endpoint)[1]
    player["Status"] = "paused"
    await service.update_metadata(backend.metadata(service.endpoint))
    assert service.track().image == image
    player["Status"] = "stopped"
    await service.update_metadata(backend.metadata(service.endpoint))
    assert service.track().image is None
    player["Status"] = "playing"
    await service.update_metadata(backend.metadata(service.endpoint))
    await until(lambda: service.artwork_url is not None)
    assert service.track().image == image
    assert client.fetch.await_count == 2
    await service.disconnected()
    client.close.assert_awaited_once()
    assert not service.artwork.images and service.artwork.task is None


async def test_no_obex_support_or_malformed_handle_keeps_text_metadata(receiver):
    service, backend, client = receiver
    player = backend.player(service.endpoint)[1]
    del player["ObexPort"]
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    assert service.track().title == "First" and service.track().image is None
    assert service.artwork.task is None
    player["ObexPort"] = 4097
    await connect_artwork(service, backend, client)
    await metadata(service, backend, Title="First", ImgHandle="../image")
    await settle(service)
    assert client.fetch.await_count == 0


async def test_disconnect_cancels_download_before_next_sender(receiver):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)

    async def pending(handle):
        await asyncio.Event().wait()

    client.fetch.side_effect = pending
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    await until(lambda: client.fetch.await_count == 1)
    previous = service.endpoint
    await service.connected(replace(previous, sequence=previous.sequence + 1))
    assert service.track().image is None and service.artwork.task is None
    client.close.assert_awaited_once()


async def test_artwork_failure_does_not_end_audio(receiver, caplog):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)
    client.fetch.side_effect = RuntimeError("phone rejected cover art")
    service.audio(b"123456")
    await settle(service)
    hold, source = service.hold, service.source
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    await until(lambda: client.close.await_count == 1)
    service.audio(b"abcdef")
    assert service.hold is hold and hold.active and not source.closed
    assert source.data.endswith(b"abcdef") and service.output_error is None
    assert service.track().title == "First" and service.track().image is None
    assert "phone rejected cover art" in caplog.text


async def test_cache_is_bounded_and_artwork_missing_on_new_track(receiver):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)
    for index in range(6):
        client.fetch.return_value = (JPEG + bytes([index]), "image/jpeg")
        await metadata(service, backend, Title=str(index), ImgHandle=f"{index:07d}")
        assert service.track().image is None
        await until(lambda: service.artwork_url is not None)
    assert len(service.artwork.images) == 4
    await metadata(service, backend, Title="No artwork")
    assert service.track().image is None


@pytest.fixture
def obex(tmp_path):
    client = ObexClient()
    client.directory = SimpleNamespace(name=str(tmp_path))
    client.session = "/session"
    return client


@pytest.mark.parametrize("data,mime", [(JPEG, "image/jpeg"), (PNG, "image/png")])
async def test_obex_waits_for_complete_uses_requested_file_and_cleans_it(
    obex, data, mime
):
    async def call(path, interface, member, signature="", body=None):
        if member == "GetThumbnail":
            Path(body[0]).write_bytes(data)
            # BlueZ can emit completion and remove the transfer before its
            # method reply reaches us. There is then no GetAll target to poll.
            obex._message(
                Message.new_signal(
                    "/transfer",
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    "sa{sv}as",
                    [TRANSFER, {"Status": Variant("s", "complete")}, []],
                )
            )
            return ["/transfer", {"Status": "active", "Filename": "/etc/passwd"}]
        pytest.fail(f"Unexpected OBEX method {member}")

    obex.call = AsyncMock(side_effect=call)
    assert await obex._download("0000001") == (data, mime)
    assert not (Path(obex.directory.name) / "cover").exists()


@pytest.mark.parametrize(
    "props", [{"Status": "error"}, {"Status": "active", "Size": MAX_IMAGE_BYTES + 1}]
)
async def test_obex_transfer_errors_cancel_and_remove_partial_files(obex, props):
    async def call(path, interface, member, signature="", body=None):
        if member == "GetThumbnail":
            Path(body[0]).write_bytes(JPEG)
            return ["/transfer", props]
        assert (path, interface, member) == ("/transfer", TRANSFER, "Cancel")
        return []

    obex.call = AsyncMock(side_effect=call)
    with pytest.raises((RuntimeError, ValueError)):
        await obex._download("0000001")
    assert obex.call.await_count == 2
    assert not (Path(obex.directory.name) / "cover").exists()


@pytest.mark.parametrize(
    "data", [b"<html>not an image</html>", JPEG + b"x" * MAX_IMAGE_BYTES]
)
async def test_obex_rejects_invalid_or_oversized_content(obex, data):
    async def call(path, interface, member, signature="", body=None):
        if member == "GetThumbnail":
            Path(body[0]).write_bytes(data)
            return ["/transfer", {"Status": "complete"}]
        return []

    obex.call = AsyncMock(side_effect=call)
    with pytest.raises(ValueError):
        await obex._download("0000001")
    assert not (Path(obex.directory.name) / "cover").exists()


async def test_native_descriptor_and_thumbnail_fallback(obex):
    obex.call = AsyncMock(
        return_value=[
            [
                {
                    "type": "native",
                    "encoding": "PNG",
                    "pixel": "600*600",
                    "size": 1000,
                }
            ]
        ]
    )
    obex._download = AsyncMock(
        side_effect=[RuntimeError("unsupported"), (JPEG, "image/jpeg")]
    )
    assert await obex.fetch("0000001") == (JPEG, "image/jpeg")
    descriptor = obex._download.call_args_list[0].args[1]
    assert (
        descriptor["encoding"].value == "PNG" and descriptor["pixel"].value == "600*600"
    )
    assert obex._download.call_args_list[1].args == ("0000001",)


async def test_cancelled_obex_download_aborts_and_removes_partial_file(obex):
    started = asyncio.Event()

    async def call(path, interface, member, signature="", body=None):
        if member == "GetThumbnail":
            Path(body[0]).write_bytes(JPEG)
            started.set()
            return ["/transfer", {"Status": "active"}]
        assert (path, interface, member) == ("/transfer", TRANSFER, "Cancel")
        return []

    obex.call = AsyncMock(side_effect=call)
    downloading = asyncio.create_task(obex._download("0000001"))
    await started.wait()
    downloading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await downloading
    assert obex.call.await_count == 2
    assert not (Path(obex.directory.name) / "cover").exists()
    assert not obex._watching and not obex._transfers


async def test_artwork_arriving_during_output_acquisition_is_shown(receiver):
    service, backend, client = receiver
    await connect_artwork(service, backend, client)
    ready, resume = asyncio.Event(), asyncio.Event()
    acquire = service.direct.acquire

    async def delayed(*args, **kwargs):
        hold = await acquire(*args, **kwargs)
        play = hold.play_live

        async def play_live(*args, **kwargs):
            await play(*args, **kwargs)
            ready.set()
            await resume.wait()

        hold.play_live = play_live
        return hold

    service.direct.acquire = delayed
    service.audio(b"123456")
    await ready.wait()
    await metadata(service, backend, Title="First", ImgHandle="0000001")
    await until(lambda: service.artwork_url is not None)
    resume.set()
    await until(lambda: service.hold is not None)
    assert service.hold.shown[-1][0].image is not None
    assert len(service.hold.plays) == 1


@pytest.mark.skipif(
    shutil.which("dbus-daemon") is None, reason="needs private dbus-daemon"
)
@pytest.mark.parametrize("port", [4097, 4099, 4101])
async def test_private_bus_obex_protocol_and_child_cleanup(tmp_path, monkeypatch, port):
    executable = tmp_path / "obexd"
    fixture = Path(__file__).parent / "fixtures" / "obexd.py"
    executable.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    executable.chmod(0o755)
    which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: str(executable) if name == "obexd" else which(name),
    )
    client = ObexClient()
    try:
        if port == 4099:
            with pytest.raises(RuntimeError, match="CreateSession"):
                await client.connect("00:11:22:33:44:55", port, "AA:BB:CC:DD:EE:FF")
        else:
            await client.connect("00:11:22:33:44:55", port, "AA:BB:CC:DD:EE:FF")
            assert await client.fetch("0000001") == (PNG, "image/png")
            if port == 4101:
                await client.call(
                    client.session, "org.bluez.obex.Session1", "DropSession"
                )
                await until(lambda: client._session_lost)
                with pytest.raises(RuntimeError, match="session disconnected"):
                    client.check_alive()
        directory = Path(client.directory.name)
        assert json.loads((directory / "request.json").read_text()) == {
            "address": "00:11:22:33:44:55",
            "PSM": port,
            "Source": "AA:BB:CC:DD:EE:FF",
            "Target": "bip-avrcp",
        }
        daemon, obexd = client.daemon, client.obexd
        # Observe protocol cleanup before the temporary directory is removed.
        cleanup = client.directory.cleanup

        def checked_cleanup():
            if port != 4099:
                assert (directory / "removed").exists()
            cleanup()

        client.directory.cleanup = checked_cleanup
    finally:
        await client.close()
    assert daemon.returncode is not None and obexd.returncode is not None
    assert not directory.exists() and client.bus is None
