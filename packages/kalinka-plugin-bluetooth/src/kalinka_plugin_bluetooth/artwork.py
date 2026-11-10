"""AVRCP cover art via BlueZ's OBEX BIP client, independent of PCM capture."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import hashlib
import io
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from kalinka_plugin_sdk.inputmodule import ContentInfo

from .backend import ADAPTER, OBJECTS, PROPERTIES, plain

logger = logging.getLogger(__name__)
OBEX = "org.bluez.obex"
CLIENT = "org.bluez.obex.Client1"
IMAGE = "org.bluez.obex.Image1"
TRANSFER = "org.bluez.obex.Transfer1"
SESSION = "org.bluez.obex.Session1"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
CLEANUP_TIMEOUT = 1.0
PROCESS_EXIT_TIMEOUT = 2.0
HEALTH_CHECK_INTERVAL = 2.0
RETRY_DELAY = 10.0


async def _with_timeout(awaitable, timeout):
    """@brief Bound a wait without losing caller cancellation on Python 3.11."""
    # wait_for can swallow cancellation when its child completes simultaneously.
    async with asyncio.timeout(timeout):
        return await awaitable


async def _finish_cleanup(future):
    """Finish bounded cleanup before propagating even repeated cancellation."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ObexClient:
    """Own a private bus/obexd so headless Core needs no user login session.

    No incoming-transfer agent or auto-accept is enabled. All processes and
    temporary files belong to Core and are retired on sender disconnect.
    """

    def __init__(self):
        self.bus = self.daemon = self.obexd = self.directory = None
        self.session = None
        self._watching = False
        self._transfers = {}
        self._progress = asyncio.Event()
        self._session_lost = False
        self._closing = None

    def _message(self, message):
        if (
            message.message_type == MessageType.SIGNAL
            and message.interface == OBJECTS
            and message.member == "InterfacesRemoved"
            and len(message.body) == 2
            and message.body[0] == self.session
            and {SESSION, IMAGE}.intersection(message.body[1])
        ):
            self._session_lost = True
        if (
            self._watching
            and message.message_type == MessageType.SIGNAL
            and message.interface == PROPERTIES
            and message.member == "PropertiesChanged"
            and len(message.body) >= 2
            and message.body[0] == TRANSFER
        ):
            self._transfers.setdefault(message.path, {}).update(plain(message.body[1]))
            self._progress.set()

    def check_alive(self):
        if self._session_lost:
            raise RuntimeError("Bluetooth artwork session disconnected")
        if self.bus is None or not self.bus.connected:
            raise RuntimeError("Bluetooth artwork bus disconnected")
        if any(
            process is not None and process.returncode is not None
            for process in (self.obexd, self.daemon)
        ):
            raise RuntimeError("Bluetooth artwork daemon exited")

    async def call(self, path, interface, member, signature="", body=None):
        if self.obexd is not None and self.obexd.returncode is not None:
            raise RuntimeError("Bluetooth artwork daemon exited")
        reply = await _with_timeout(
            self.bus.call(
                Message(
                    destination=OBEX,
                    path=path,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body or [],
                )
            ),
            10,
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"OBEX {member}: {reply.error_name}")
        return plain(reply.body)

    async def connect(self, address, port, source):
        executable = next(
            (
                p
                for p in (
                    shutil.which("obexd"),
                    "/usr/libexec/bluetooth/obexd",
                    "/usr/lib/bluetooth/obexd",
                    "/usr/libexec/obexd",
                )
                if p and os.access(p, os.X_OK)
            ),
            None,
        )
        if executable is None:
            raise RuntimeError("Install bluez-obexd for Bluetooth artwork")
        self.directory = tempfile.TemporaryDirectory(prefix="kalinka-bip-")
        self.daemon = await asyncio.create_subprocess_exec(
            "dbus-daemon",
            "--session",
            "--nofork",
            "--print-address=1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        bus_address = (
            (await _with_timeout(self.daemon.stdout.readline(), 5)).decode().strip()
        )
        if not bus_address:
            raise RuntimeError("Could not start the Bluetooth artwork bus")
        self.bus = await _with_timeout(MessageBus(bus_address=bus_address).connect(), 5)
        self.bus.add_message_handler(self._message)
        for interface, member in (
            (PROPERTIES, "PropertiesChanged"),
            (OBJECTS, "InterfacesRemoved"),
        ):
            reply = await _with_timeout(
                self.bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="AddMatch",
                        signature="s",
                        body=[
                            f"type='signal',sender='{OBEX}',interface='{interface}',member='{member}'"
                        ],
                    )
                ),
                5,
            )
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError("Could not subscribe to Bluetooth artwork transfers")
        self.obexd = await asyncio.create_subprocess_exec(
            # obexd requires a server driver and transport even for client use.
            # Keep its minimal OPP driver; without an agent, pushes are denied.
            executable,
            "--nodetach",
            "--plugin=opp,bluetooth",
            env={
                **os.environ,
                "DBUS_SESSION_BUS_ADDRESS": bus_address,
                "XDG_CACHE_HOME": self.directory.name,
            },
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Wait for our daemon, avoiding activation of a second obexd.
        async with asyncio.timeout(5):
            while True:
                if self.obexd.returncode is not None:
                    raise RuntimeError("Bluetooth artwork daemon exited")
                reply = await self.bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="NameHasOwner",
                        signature="s",
                        body=[OBEX],
                    )
                )
                if reply.body == [True]:
                    break
                await asyncio.sleep(0.05)
        args = {"Target": Variant("s", "bip-avrcp"), "PSM": Variant("q", port)}
        if source:
            args["Source"] = Variant("s", source)
        self.session = (
            await self.call(
                "/org/bluez/obex", CLIENT, "CreateSession", "sa{sv}", [address, args]
            )
        )[0]

    async def _download(self, handle, description=None):
        target = Path(self.directory.name) / "cover"
        transfer = None
        complete = False
        self._transfers.clear()
        self._watching = True
        try:
            async with asyncio.timeout(15):
                method = "GetThumbnail" if description is None else "Get"
                args = [str(target), handle]
                if description is not None:
                    args.append(description)
                transfer, props = await self.call(
                    self.session,
                    IMAGE,
                    method,
                    "ss" if description is None else "ssa{sv}",
                    args,
                )
                while True:
                    self._progress.clear()
                    # obexd removes a transfer immediately on completion. Keep
                    # signals even when they arrive before Get's method reply;
                    # polling GetAll alone can miss every successful transfer.
                    props.update(self._transfers.get(transfer, {}))
                    if (
                        max(props.get("Size", 0), props.get("Transferred", 0))
                        > MAX_IMAGE_BYTES
                    ):
                        raise ValueError("Bluetooth artwork exceeds 4 MiB")
                    if target.exists() and target.stat().st_size > MAX_IMAGE_BYTES:
                        raise ValueError("Bluetooth artwork exceeds 4 MiB")
                    if props.get("Status") == "complete":
                        complete = True
                        break
                    if props.get("Status") == "error":
                        raise RuntimeError("Bluetooth artwork transfer failed")
                    with suppress(TimeoutError):
                        await _with_timeout(self._progress.wait(), 0.1)
                # Never follow a remote-supplied Filename or return a file URL.
                with target.open("rb") as stream:
                    data = stream.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("Bluetooth artwork exceeds 4 MiB")
                if data.startswith(b"\xff\xd8\xff"):
                    return data, "image/jpeg"
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    return data, "image/png"
                raise ValueError("Bluetooth artwork is not JPEG or PNG")
        finally:
            self._watching = False
            self._transfers.clear()
            try:
                if transfer and not complete:
                    with suppress(Exception):
                        async with asyncio.timeout(CLEANUP_TIMEOUT):
                            await self.call(transfer, TRANSFER, "Cancel")
            finally:
                target.unlink(missing_ok=True)

    async def fetch(self, handle):
        # Explicit descriptors also work with early BlueZ BIP implementations
        # that do not accept an empty native-image description.
        try:
            properties = (
                await self.call(self.session, IMAGE, "Properties", "s", [handle])
            )[0]
            for prop in properties:
                if (
                    prop.get("type") == "native"
                    and prop.get("encoding") in ("JPEG", "PNG")
                    and prop.get("pixel")
                    and prop.get("size", 0) <= MAX_IMAGE_BYTES
                ):
                    return await self._download(
                        handle,
                        {key: Variant("s", prop[key]) for key in ("encoding", "pixel")},
                    )
        except (RuntimeError, TimeoutError, ValueError, OSError):
            pass
        # The linked JPEG thumbnail is the AVRCP interoperability baseline.
        return await self._download(handle)

    async def close(self):
        # One owner performs teardown; cancellation of any caller must not
        # interrupt RemoveSession, bus disconnect, or child termination/reaping.
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await _finish_cleanup(self._closing)

    async def _close(self):
        try:
            if self.session and self.bus and self.bus.connected:
                with suppress(Exception):
                    async with asyncio.timeout(CLEANUP_TIMEOUT):
                        await self.call(
                            "/org/bluez/obex",
                            CLIENT,
                            "RemoveSession",
                            "o",
                            [self.session],
                        )
        finally:
            self.session = None
            try:
                if self.bus:
                    self.bus.disconnect()
                    with suppress(Exception):
                        async with asyncio.timeout(CLEANUP_TIMEOUT):
                            await self.bus.wait_for_disconnect()
                    self.bus = None
            finally:
                await self._stop_children()
                if self.directory:
                    self.directory.cleanup()
                    self.directory = None

    async def _stop_children(self):
        # Signal both children before waiting, so a slow one cannot delay
        # termination of the other. Reap children that exited on their own too.
        for process in (self.obexd, self.daemon):
            if process and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()

        async def reap(process):
            if process is not None:
                try:
                    await _with_timeout(process.wait(), PROCESS_EXIT_TIMEOUT)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

        results = await asyncio.gather(
            *(reap(process) for process in (self.obexd, self.daemon)),
            return_exceptions=True,
        )
        self.obexd = self.daemon = None
        for result in results:
            if isinstance(result, BaseException):
                raise result


class BluetoothArtwork:
    """Keep a BIP session open before ImgHandle arrives; publish current art only."""

    def __init__(self, backend, publish, *, client_factory=ObexClient):
        self.backend, self.publish = backend, publish
        self.client_factory = client_factory
        self.images = OrderedDict()
        self.desired = None
        self.task = None
        self.changed = asyncio.Event()

    def update(self, endpoint, metadata):
        player = self.backend.player(endpoint) if endpoint else None
        port = player[1].get("ObexPort") if player else None
        desired = None
        if isinstance(port, int) and 0 < port <= 65535:
            adapter = self.backend.bluez.get(
                f"/org/bluez/{self.backend.adapter}", {}
            ).get(ADAPTER, {})
            connection = (
                endpoint.identity,
                player[0],
                endpoint.address,
                port,
                adapter.get("Address", ""),
            )
            desired = (connection, dict(metadata))
        changed = desired != self.desired
        if changed:
            self.desired = desired
        self.changed.set()
        if self.task is None and desired is not None:
            self.task = asyncio.create_task(self._run())
        return changed

    def content(self, asset_id):
        entry = self.images.get(asset_id)
        if entry is None:
            return None
        data, mime = entry
        return ContentInfo(
            mime_type=mime,
            size=len(data),
            reader=lambda: io.BytesIO(data),
            cacheable=True,
        )

    async def _run(self):
        client = connection = fetched = None
        last_error = None
        try:
            while True:
                self.changed.clear()
                request = self.desired
                try:
                    if client and (request is None or request[0] != connection):
                        await client.close()
                        client = connection = fetched = None
                    if request:
                        if client is not None:
                            # A lost BIP connection can remove ImgHandle. Check
                            # health even when there is no image left to fetch.
                            client.check_alive()
                        if client is None:
                            client = self.client_factory()
                            connection = request[0]
                            await client.connect(*connection[2:])
                            self.backend.changed.set()  # BlueZ can now expose ImgHandle.
                            # Handles from before CreateSession may have expired.
                            # Wait for the next BlueZ snapshot before using one.
                            while not self.changed.is_set():
                                with suppress(TimeoutError):
                                    await _with_timeout(
                                        self.changed.wait(), HEALTH_CHECK_INTERVAL
                                    )
                                client.check_alive()
                            continue
                        handle = request[1].get("ImgHandle", "")
                        if (
                            isinstance(handle, str)
                            and re.fullmatch(r"[0-9]{7}", handle)
                            and request is not fetched
                        ):
                            data, mime = await client.fetch(handle)
                            # Snapshot identity also detects A -> B -> A while
                            # a download is pending, or stop/resume of A.
                            if request is self.desired:
                                asset_id = "art-" + hashlib.sha256(data).hexdigest()
                                self.images[asset_id] = (data, mime)
                                self.images.move_to_end(asset_id)
                                while len(self.images) > 4:
                                    self.images.popitem(last=False)
                                await self.publish(f"/content/bluetooth/{asset_id}")
                                fetched = request
                                last_error = None
                except Exception as error:
                    if str(error) != last_error:
                        logger.warning("Bluetooth artwork unavailable: %s", error)
                        last_error = str(error)
                    # A fresh connection can recover obexd or a lost BIP link.
                    if client:
                        await client.close()
                    client = connection = fetched = None
                    # Bound retries even if metadata events arrive continuously.
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                if self.desired is request:
                    with suppress(TimeoutError):
                        await _with_timeout(self.changed.wait(), HEALTH_CHECK_INTERVAL)
        finally:
            if client:
                await client.close()

    async def close(self):
        self.desired = None
        self.images.clear()
        worker = self.task
        if worker:
            # A second close must not cancel the worker again during cleanup.
            if not worker.cancelling():
                worker.cancel()
            try:
                await _finish_cleanup(asyncio.gather(worker, return_exceptions=True))
            finally:
                if self.task is worker:
                    self.task = None
