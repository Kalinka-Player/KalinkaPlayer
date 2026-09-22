"""Finding the file servers on this network.

Two ways, because no single one sees every server. Apple's and most NAS
boxes' shares announce themselves over mDNS as ``_smb._tcp``, which costs
nothing to listen to and arrives by itself. Windows machines and plain Samba
installs answer a NetBIOS node-status broadcast instead, which has to be
asked for and is therefore asked rarely.

Neither is allowed to hold up the settings page. What has answered so far is
kept here and handed over at once; a request for something fresher only
starts the next broadcast. The list is a starting point either way — a host
is not yet a music folder, because the share on it still has to be named.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

logger = logging.getLogger(__name__.split(".")[-1])

MDNS_SERVICE = "_smb._tcp.local."

#: NetBIOS Name Service. Its own port, and the only one a node-status
#: request is answered on.
_NBNS_PORT = 137

#: Node status request, for every name a host has registered.
_NBSTAT = 0x0021
_IN_CLASS = 0x0001

#: The name every host answers a node-status request for.
_WILDCARD = "*"

#: The NetBIOS suffix of the file-sharing service. A host registers it only
#: while it is actually serving shares, which is the question being asked.
_SERVER_SERVICE = 0x20

#: SIOCGIFBRDADDR — the broadcast address of one interface.
_SIOCGIFBRDADDR = 0x8919

#: How long one broadcast's replies are collected. A host that has not
#: answered by then is left to the next scan rather than waited for.
_SCAN_SECONDS = 2.5

#: Floor between broadcasts, however often suggestions are asked for. The
#: settings page asks on every read, and a scan is traffic on someone's
#: network.
_MIN_SCAN_INTERVAL_S = 10.0

#: How long a host that answered once stays on the list without answering
#: again. Long enough to survive a few missed replies, short enough that a
#: machine taken off the network stops being offered.
_STALE_S = 600.0


@dataclass(frozen=True)
class DiscoveredHost:
    """A file server seen on the network.

    @param address What an SMB client connects to — always an address,
        never a name, because a name found by mDNS or NetBIOS is not one
        this machine's resolver can necessarily look up.
    @param name What the host calls itself, empty when it did not say.
    @param source How it was found, shown to the user so two machines with
        the same name can be told apart.
    """

    address: str
    name: str
    source: str


def encode_netbios_name(name: str, suffix: int = 0x00) -> bytes:
    """A NetBIOS name in the half-ASCII encoding the wire uses.

    Sixteen bytes — fifteen of name, then the service suffix — with each
    nibble sent as a letter from 'A'. The result is the 32 bytes that go
    into a question, without its length prefix.

    @note A name is padded with spaces, the wildcard with nulls. Get that
        backwards and no host recognises the name it is asked about.
    """
    raw = name.upper().encode("ascii", "replace")[:15]
    padded = raw.ljust(15, b"\x00" if name == _WILDCARD else b" ")
    padded += bytes([suffix])
    out = bytearray()
    for byte in padded:
        out.append(ord("A") + (byte >> 4))
        out.append(ord("A") + (byte & 0x0F))
    return bytes(out)


def nbstat_query(transaction_id: int = 0) -> bytes:
    """A node-status request for every name on whoever receives it."""
    header = struct.pack(
        ">HHHHHH",
        transaction_id & 0xFFFF,
        0x0010,  # broadcast
        1,  # one question
        0,
        0,
        0,
    )
    name = encode_netbios_name(_WILDCARD)
    return header + bytes([len(name)]) + name + b"\x00" + struct.pack(
        ">HH", _NBSTAT, _IN_CLASS
    )


def _skip_name(data: bytes, offset: int) -> int:
    """Past one length-prefixed name, to whatever follows it."""
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0:  # a pointer, which ends the name
            return offset + 2
        offset += 1 + length
    raise ValueError("the name runs past the end of the packet")


def parse_nbstat_reply(data: bytes) -> list[tuple[str, int, bool]]:
    """The name table out of a node-status reply.

    @return One ``(name, suffix, is_group)`` per registered name.
    @raise ValueError If the packet is not a node-status reply, or is cut
        short of the table it claims to carry.
    """
    if len(data) < 12:
        raise ValueError("the packet is shorter than a header")
    _id, flags, _qd, answers, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if not flags & 0x8000 or answers < 1:
        raise ValueError("not an answer")

    offset = _skip_name(data, 12)
    if len(data) < offset + 10:
        raise ValueError("the answer is cut short")
    rr_type, _rr_class, _ttl, _rdlength = struct.unpack(
        ">HHIH", data[offset:offset + 10]
    )
    if rr_type != _NBSTAT:
        raise ValueError("the answer is not a node status")
    offset += 10

    if len(data) <= offset:
        raise ValueError("the name table is missing")
    count = data[offset]
    offset += 1
    if len(data) < offset + count * 18:
        raise ValueError("the name table is cut short")

    names = []
    for _ in range(count):
        raw = data[offset:offset + 15]
        suffix = data[offset + 15]
        (entry_flags,) = struct.unpack(">H", data[offset + 16:offset + 18])
        offset += 18
        name = raw.decode("ascii", "replace").rstrip(" \x00")
        names.append((name, suffix, bool(entry_flags & 0x8000)))
    return names


def file_server_name(names: Iterable[tuple[str, int, bool]]) -> Optional[str]:
    """The name a host serves shares under, or None when it serves none."""
    for name, suffix, is_group in names:
        if suffix == _SERVER_SERVICE and not is_group and name:
            return name
    return None


def broadcast_addresses() -> list[str]:
    """Where to send a broadcast so every interface's network hears it.

    Falls back to the all-networks address, which reaches the default route
    alone but is better than asking nobody.
    """
    try:
        import fcntl
    except ImportError:
        return ["255.255.255.255"]

    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _index, name in socket.if_nameindex():
                try:
                    packed = fcntl.ioctl(
                        probe.fileno(),
                        _SIOCGIFBRDADDR,
                        struct.pack("256s", name.encode()[:15]),
                    )
                except OSError:
                    continue
                address = socket.inet_ntoa(packed[20:24])
                if address == "0.0.0.0" or address.startswith("127."):
                    continue
                if address not in found:
                    found.append(address)
    except OSError as exc:
        logger.debug("Cannot enumerate broadcast addresses: %s", exc)
    return found or ["255.255.255.255"]


def _is_connectable(address: str) -> bool:
    """Whether an SMB client on this machine could reach a share there.

    Loopback is this machine answering its own announcement — the shares
    behind it are its own folders, which it has already offered as folders.
    An IPv6 link-local address needs the interface zone that an
    announcement does not carry.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not parsed.is_loopback and not (
        parsed.version == 6 and parsed.is_link_local
    )


class MdnsWatch(Protocol):
    """A running subscription to the network's ``_smb._tcp`` announcements."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


MdnsFactory = Callable[
    [Callable[[str, str, list[str]], None], Callable[[str], None]], MdnsWatch
]


class _ZeroconfWatch:
    """mDNS announcements, through the zeroconf package.

    @note Its callbacks arrive on zeroconf's own thread, so resolving a
        service is allowed to block there and nothing else waits on it.
    """

    def __init__(
        self,
        on_seen: Callable[[str, str, list[str]], None],
        on_lost: Callable[[str], None],
    ) -> None:
        self._on_seen = on_seen
        self._on_lost = on_lost
        self._zeroconf = None
        self._browser = None

    def start(self) -> None:
        from zeroconf import ServiceBrowser, Zeroconf

        self._zeroconf = Zeroconf()
        try:
            self._browser = ServiceBrowser(self._zeroconf, MDNS_SERVICE, self)
        except Exception:
            # The engine is already listening on the multicast group by now,
            # and the caller drops this object on a failed start — so it has
            # to let go of its sockets here or nothing ever will.
            self.stop()
            raise

    def stop(self) -> None:
        # The browser is cancelled and the listener closed, in that order and
        # by those names: a browser is a thread of its own, and closing the
        # listener under it leaves it running against a socket that is gone.
        for part, release in ((self._browser, "cancel"), (self._zeroconf, "close")):
            if part is None:
                continue
            try:
                getattr(part, release)()
            except Exception as exc:  # noqa: BLE001 — teardown, never fatal
                logger.warning("Stopping the mDNS browser failed: %s", exc)
        self._browser = None
        self._zeroconf = None

    # zeroconf's ServiceListener interface
    def add_service(self, zeroconf, service_type: str, name: str) -> None:
        info = zeroconf.get_service_info(service_type, name, timeout=1500)
        if info is None:
            return
        # parsed_addresses(), not addresses: the latter is IPv4 only, so a
        # server that answers over v6 alone would never be offered.
        addresses = [
            address for address in info.parsed_addresses() if _is_connectable(address)
        ]
        if addresses:
            self._on_seen(name, name.split(".")[0], addresses)

    def update_service(self, zeroconf, service_type: str, name: str) -> None:
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type: str, name: str) -> None:
        self._on_lost(name)


class SmbHostDiscovery:
    """The file servers this machine can see, kept up to date in the
    background.

    @note Owned by the process that serves the settings page. Starting a
        second one would put a second listener on the multicast group and
        a second broadcast on the network for the same answer.
    """

    def __init__(
        self,
        mdns_factory: MdnsFactory = _ZeroconfWatch,
        scan_seconds: float = _SCAN_SECONDS,
        min_scan_interval: float = _MIN_SCAN_INTERVAL_S,
        stale_after: float = _STALE_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mdns_factory = mdns_factory
        self._scan_seconds = scan_seconds
        self._min_scan_interval = min_scan_interval
        self._stale_after = stale_after
        self._now = now

        self._lock = threading.Lock()
        # Keyed by the announcement that carried them, so withdrawing one
        # takes its addresses with it.
        self._announced: dict[str, list[DiscoveredHost]] = {}
        # Keyed by address, with when it last answered: a broadcast reply
        # is a moment, not a subscription, so these have to expire.
        self._answered: dict[str, tuple[DiscoveredHost, float]] = {}

        self._mdns: Optional[MdnsWatch] = None
        self._stopping = threading.Event()
        self._scanning = threading.Lock()
        self._last_scan: Optional[float] = None

    def start(self) -> None:
        """Begin listening, and ask once for what is already out there."""
        self._stopping.clear()
        if self._mdns is None:
            try:
                self._mdns = self._mdns_factory(self._mdns_seen, self._mdns_lost)
                self._mdns.start()
            except Exception as exc:  # noqa: BLE001 — one way of two
                logger.warning(
                    "Shares announced over mDNS will not be suggested: %s", exc
                )
                self._mdns = None
        self.refresh()

    def stop(self) -> None:
        self._stopping.set()
        if self._mdns is not None:
            self._mdns.stop()
            self._mdns = None

    def refresh(self) -> None:
        """Ask the network again, unless it was asked a moment ago.

        Returns immediately either way: the replies land in the background
        and show up in the next :meth:`hosts`.
        """
        if self._stopping.is_set():
            return
        now = self._now()
        if (
            self._last_scan is not None
            and now - self._last_scan < self._min_scan_interval
        ):
            return
        if not self._scanning.acquire(blocking=False):
            return
        self._last_scan = now
        try:
            threading.Thread(
                target=self._scan_and_release, name="smb-discovery", daemon=True
            ).start()
        except RuntimeError as exc:
            # Nothing will reach the release in the thread that never ran,
            # and a lock left held would retire the broadcast for good.
            self._scanning.release()
            logger.warning("No thread to scan for shares on: %s", exc)

    def hosts(self) -> list[DiscoveredHost]:
        """Every server seen recently, by address.

        A host found both ways is reported once, keeping whichever sighting
        carried a name.
        """
        cutoff = self._now() - self._stale_after
        merged: dict[str, DiscoveredHost] = {}
        with self._lock:
            for host, seen_at in self._answered.values():
                if seen_at >= cutoff:
                    merged[host.address] = host
            for announced in self._announced.values():
                for host in announced:
                    known = merged.get(host.address)
                    merged[host.address] = (
                        host if host.name or known is None else known
                    )
        return sorted(merged.values(), key=lambda h: (h.name or h.address))

    def _mdns_seen(self, key: str, name: str, addresses: list[str]) -> None:
        with self._lock:
            self._announced[key] = [
                DiscoveredHost(address=address, name=name, source="mDNS")
                for address in addresses
            ]

    def _mdns_lost(self, key: str) -> None:
        with self._lock:
            self._announced.pop(key, None)

    def _netbios_seen(self, address: str, name: str) -> None:
        with self._lock:
            self._answered[address] = (
                DiscoveredHost(address=address, name=name, source="NetBIOS"),
                self._now(),
            )

    def _scan_and_release(self) -> None:
        try:
            self._scan_once()
        except OSError as exc:
            logger.debug("The NetBIOS scan could not be sent: %s", exc)
        finally:
            self._scanning.release()

    def _scan_once(self) -> None:
        """Broadcast one node-status request and collect what answers."""
        query = nbstat_query(transaction_id=int(self._now()) & 0xFFFF)
        deadline = time.monotonic() + self._scan_seconds
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.5)
            for address in broadcast_addresses():
                try:
                    sock.sendto(query, (address, _NBNS_PORT))
                except OSError as exc:
                    logger.debug("No NetBIOS query to %s: %s", address, exc)

            while not self._stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                sock.settimeout(min(0.5, remaining))
                try:
                    data, (address, _port) = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as exc:
                    logger.debug("The NetBIOS scan stopped early: %s", exc)
                    return
                try:
                    name = file_server_name(parse_nbstat_reply(data))
                except ValueError as exc:
                    logger.debug("Ignoring a reply from %s: %s", address, exc)
                    continue
                if name is not None:
                    self._netbios_seen(address, name)
