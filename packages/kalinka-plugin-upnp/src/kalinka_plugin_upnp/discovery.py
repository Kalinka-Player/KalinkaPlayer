"""IPv4 SSDP announcements and bounded M-SEARCH responses."""

import asyncio
import logging
import random
import socket
from email.utils import formatdate

from .services import DEVICE_TYPE, SERVICES

MULTICAST = ("239.255.255.250", 1900)
SERVER = "Kalinka/1.0 UPnP/1.0 Kalinka-UPnP/1.0"
MAX_AGE = 1800
logger = logging.getLogger(__name__)


def local_address(configured: str) -> str:
    if configured:
        return configured
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(MULTICAST)
        return sock.getsockname()[0]


class Discovery(asyncio.DatagramProtocol):
    """Advertise one receiver on a selected IPv4 interface until closed."""

    def __init__(self, host, location, udn):
        self.host, self.location, self.udn = host, location, udn
        self.targets = (
            "upnp:rootdevice",
            udn,
            DEVICE_TYPE,
            *(service.type for service in SERVICES.values()),
        )
        self.transport = self.announcer = None
        self.pending = set()

    async def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", MULTICAST[1]))
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(MULTICAST[0]) + socket.inet_aton(self.host),
            )
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.host)
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setblocking(False)
            await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: self, sock=sock
            )
        except BaseException:
            sock.close()
            raise
        self.advertise("ssdp:alive")
        self.announcer = asyncio.create_task(self._announce())

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        logger.warning("UPnP discovery network error (%s)", type(exc).__name__)

    def usn(self, target):
        return self.udn if target == self.udn else f"{self.udn}::{target}"

    def send(self, line, headers, address):
        if self.transport:
            packet = "\r\n".join(
                (line, *(f"{key}: {value}" for key, value in headers.items()), "", "")
            )
            self.transport.sendto(packet.encode("utf-8"), address)

    def advertise(self, kind):
        for target in self.targets:
            headers = {
                "HOST": f"{MULTICAST[0]}:{MULTICAST[1]}",
                "NT": target,
                "NTS": kind,
                "USN": self.usn(target),
            }
            if kind == "ssdp:alive":
                headers.update(
                    {
                        "CACHE-CONTROL": f"max-age={MAX_AGE}",
                        "LOCATION": self.location,
                        "SERVER": SERVER,
                    }
                )
            self.send("NOTIFY * HTTP/1.1", headers, MULTICAST)

    def datagram_received(self, data, address):
        if len(data) > 8192 or len(self.pending) >= 64:
            return
        try:
            lines = data.decode("ascii").split("\r\n")
            if lines[0] != "M-SEARCH * HTTP/1.1":
                return
            headers = {
                key.strip().lower(): value.strip()
                for key, value in (line.split(":", 1) for line in lines[1:] if line)
            }
            if headers.get("man") != '"ssdp:discover"':
                return
            mx = min(5, int(headers["mx"]))
            if mx < 1:
                return
            target = headers["st"]
        except (UnicodeError, ValueError, KeyError):
            return
        targets = (
            self.targets
            if target == "ssdp:all"
            else (target,)
            if target in self.targets
            else ()
        )
        if not targets:
            return

        def reply():
            self.pending.discard(handle)
            for target in targets:
                self.send(
                    "HTTP/1.1 200 OK",
                    {
                        "CACHE-CONTROL": f"max-age={MAX_AGE}",
                        "DATE": formatdate(usegmt=True),
                        "EXT": "",
                        "LOCATION": self.location,
                        "SERVER": SERVER,
                        "ST": target,
                        "USN": self.usn(target),
                    },
                    address,
                )

        handle = asyncio.get_running_loop().call_later(random.uniform(0, mx), reply)
        self.pending.add(handle)

    async def _announce(self):
        while True:
            await asyncio.sleep(MAX_AGE / 2)
            self.advertise("ssdp:alive")

    async def close(self):
        if self.announcer:
            self.announcer.cancel()
            await asyncio.gather(self.announcer, return_exceptions=True)
        for handle in self.pending:
            handle.cancel()
        self.pending.clear()
        if self.transport:
            try:
                self.advertise("ssdp:byebye")
            finally:
                self.transport.close()
                self.transport = None
