"""Finding the file servers on this network.

Three ways, because no single one sees every server. Apple's and most NAS
boxes' shares announce themselves over mDNS as ``_smb._tcp``, which costs
nothing to listen to and arrives by itself. Windows answers a WS-Discovery
probe for computers, and Samba a NetBIOS name query for every name; both
have to be asked for, and are therefore asked rarely. Neither answer says
what the host is called or whether it serves shares, so each host that
answers is asked that on its own address with a node-status request — one
sent by broadcast goes unanswered by Windows and Samba alike.

None of it is allowed to hold up the settings page. What has answered so far
is kept here and handed over at once; a request for something fresher only
starts the next scan. The list is a starting point either way — a host is
not yet a music folder, because the share on it still has to be named.
"""

from __future__ import annotations

import ipaddress
import logging
import select
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Protocol

logger = logging.getLogger(__name__.split(".")[-1])

MDNS_SERVICE = "_smb._tcp.local."

#: NetBIOS Name Service. Its own port, and the only one a node-status
#: request is answered on.
_NBNS_PORT = 137

#: Name query, and node status request, for every name a host has
#: registered.
_NB = 0x0020
_NBSTAT = 0x0021
_IN_CLASS = 0x0001

#: The name every host answers a node-status request for.
_WILDCARD = "*"

#: WS-Discovery's multicast group, where a probe is answered by every
#: device of the types it names.
_WSD_GROUP = "239.255.255.250"
_WSD_PORT = 3702
_WSD_NS = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_ADDRESSING_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"

#: The type a Windows computer publishes itself under; printers, scanners
#: and cameras answer WS-Discovery too, under types of their own.
_PUB_NS = "http://schemas.microsoft.com/windows/pub/2005/07"
_COMPUTER = "Computer"

#: How each way of finding a host is named to the user.
WS_DISCOVERY = "WS-Discovery"
NETBIOS = "NetBIOS"

#: The NetBIOS suffix of the file-sharing service. A host registers it only
#: while it is actually serving shares, which is the question being asked.
_SERVER_SERVICE = 0x20

#: SIOCGIFADDR and SIOCGIFBRDADDR — the address, and the broadcast address,
#: of one interface.
_SIOCGIFADDR = 0x8915
_SIOCGIFBRDADDR = 0x8919

#: How long one scan's replies are collected. A host that has not answered
#: by then is left to the next scan rather than waited for.
_SCAN_SECONDS = 2.5

#: Floor between scans, however often suggestions are asked for. The
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


def _wildcard_question(transaction_id: int, flags: int, question_type: int) -> bytes:
    header = struct.pack(">HHHHHH", transaction_id & 0xFFFF, flags, 1, 0, 0, 0)
    name = encode_netbios_name(_WILDCARD)
    return header + bytes([len(name)]) + name + b"\x00" + struct.pack(
        ">HH", question_type, _IN_CLASS
    )


def nbstat_query(transaction_id: int = 0) -> bytes:
    """A node-status request for every name on the one host it is sent to."""
    return _wildcard_question(transaction_id, 0x0000, _NBSTAT)


def name_query(transaction_id: int = 0) -> bytes:
    """A broadcast name query for every name, which Samba answers with its
    address and Windows leaves unanswered."""
    return _wildcard_question(transaction_id, 0x0110, _NB)  # recursion, broadcast


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


def _answer(data: bytes) -> tuple[int, int]:
    """The type of the answer a name-service reply carries, and where that
    answer's data starts.

    @raise ValueError If the packet is not a positive answer, or is cut
        short of one.
    """
    if len(data) < 12:
        raise ValueError("the packet is shorter than a header")
    _id, flags, _qd, answers, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if not flags & 0x8000 or flags & 0x000F or answers < 1:
        raise ValueError("not an answer")

    offset = _skip_name(data, 12)
    if len(data) < offset + 10:
        raise ValueError("the answer is cut short")
    rr_type, _rr_class, _ttl, _rdlength = struct.unpack(
        ">HHIH", data[offset:offset + 10]
    )
    return rr_type, offset + 10


def answers_name_query(data: bytes) -> bool:
    """Whether a packet is a host answering a name query, which gives its
    address and nothing more."""
    try:
        rr_type, _offset = _answer(data)
    except ValueError:
        return False
    return rr_type == _NB


def parse_nbstat_reply(data: bytes) -> list[tuple[str, int, bool]]:
    """The name table out of a node-status reply.

    @return One ``(name, suffix, is_group)`` per registered name.
    @raise ValueError If the packet is not a node-status reply, or is cut
        short of the table it claims to carry.
    """
    rr_type, offset = _answer(data)
    if rr_type != _NBSTAT:
        raise ValueError("the answer is not a node status")

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


def wsd_probe(message_id: str) -> bytes:
    """A WS-Discovery probe for computers, under ``message_id`` (a
    ``urn:uuid:`` a reply relates itself to)."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{_SOAP_NS}" xmlns:wsa="{_ADDRESSING_NS}"'
        f' xmlns:wsd="{_WSD_NS}" xmlns:pub="{_PUB_NS}">'
        "<soap:Header>"
        "<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>"
        f"<wsa:Action>{_WSD_NS}/Probe</wsa:Action>"
        f"<wsa:MessageID>{message_id}</wsa:MessageID>"
        "</soap:Header>"
        f"<soap:Body><wsd:Probe><wsd:Types>pub:{_COMPUTER}</wsd:Types>"
        "</wsd:Probe></soap:Body></soap:Envelope>"
    ).encode()


class _ProbeReply(ElementTree.TreeBuilder):
    """A WS-Discovery reply's tree, and the namespaces it declares.

    Refuses a DTD in whatever encoding it arrives: WS-Discovery has no use
    for one, and a DTD is how entities that expand without end get in.
    """

    def __init__(self) -> None:
        super().__init__()
        self.namespaces: dict[str, str] = {}

    def start_ns(self, prefix: str, uri: str) -> None:
        self.namespaces[prefix] = uri

    def doctype(self, name: str, pubid: str, system: str) -> None:
        raise ValueError("a WS-Discovery reply carries no DTD")


def is_computer_match(data: bytes) -> bool:
    """Whether a WS-Discovery reply is a probe match from a computer.

    A type is a prefixed name, and the prefix is the sender's to choose, so
    it is looked up in the namespaces the reply declares.
    """
    reply = _ProbeReply()
    parser = ElementTree.XMLParser(target=reply)
    try:
        parser.feed(data)
        root = parser.close()
    except (ElementTree.ParseError, ValueError, LookupError):
        return False
    types = [
        name
        for match in root.iter(f"{{{_WSD_NS}}}ProbeMatch")
        for listed in match.iter(f"{{{_WSD_NS}}}Types")
        for name in (listed.text or "").split()
    ]
    return any(_is_computer_type(name, reply.namespaces) for name in types)


def _is_computer_type(name: str, namespaces: Mapping[str, str]) -> bool:
    prefix, _, local = name.rpartition(":")
    return local == _COMPUTER and namespaces.get(prefix) == _PUB_NS


def _interface_addresses(request: int) -> list[str]:
    """One IPv4 address per interface, as the ioctl ``request`` reads it,
    bar the unset and loopback ones."""
    try:
        import fcntl
    except ImportError:
        return []

    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _index, name in socket.if_nameindex():
                try:
                    packed = fcntl.ioctl(
                        probe.fileno(),
                        request,
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
        logger.debug("Cannot enumerate interface addresses: %s", exc)
    return found


def broadcast_addresses() -> list[str]:
    """Where to send a broadcast so every interface's network hears it.

    Falls back to the all-networks address, which reaches the default route
    alone but is better than asking nobody.
    """
    return _interface_addresses(_SIOCGIFBRDADDR) or ["255.255.255.255"]


def multicast_interfaces() -> list[str]:
    """This machine's IPv4 address on each interface: naming one is how a
    multicast goes out of that interface rather than the default route's."""
    return _interface_addresses(_SIOCGIFADDR)


#: Reserved for documentation (RFC 5737), so assigned to no machine
#: anywhere. One that binds it binds anything.
_ASSIGNED_NOWHERE = "192.0.2.1"


def _as_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(address)
    except ValueError:
        return None


def _belongs_to_this_machine(address: str) -> bool:
    """Whether a literal address is one this machine holds.

    Put to the kernel: a socket binds to an address only where that address
    is assigned here, whatever the interface or family. A name is refused
    rather than resolved — that is a different question, and a broken
    resolver answers it slowly.
    """
    parsed = _as_address(address)
    if parsed is None:
        return False
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


def _is_a_server_address(address: str) -> bool:
    """Whether an SMB client here could be pointed at this address at all.

    Out go the ones that name no single host — a name, the unspecified
    address, multicast, broadcast — and loopback, which is always this
    machine. An IPv6 link-local address goes too: the zone that makes one
    reachable would have to survive the smb:// URL, and does not.
    """
    parsed = _as_address(address)
    if parsed is None:
        return False
    if parsed.is_unspecified or parsed.is_multicast or parsed.is_reserved:
        return False
    if parsed.is_loopback:
        return False
    return not (parsed.version == 6 and parsed.is_link_local)


def _is_ipv4(host: DiscoveredHost) -> bool:
    parsed = _as_address(host.address)
    return parsed is not None and parsed.version == 4


def _is_routed_ipv4(host: DiscoveredHost) -> bool:
    parsed = _as_address(host.address)
    return parsed is not None and parsed.version == 4 and not parsed.is_link_local


def _without_ipv6_aliases(hosts: list[DiscoveredHost]) -> list[DiscoveredHost]:
    """The name is what ties an mDNS sighting to a NetBIOS one from the same box."""
    named_on_ipv4 = {
        h.name.casefold() for h in hosts if h.name and _is_routed_ipv4(h)
    }
    return [
        h
        for h in hosts
        if _is_ipv4(h) or not h.name or h.name.casefold() not in named_on_ipv4
    ]


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
        # An instance name is free text and may hold a dot of its own.
        self._on_seen(
            name, name.removesuffix(f".{service_type}"), info.parsed_addresses()
        )

    def update_service(self, zeroconf, service_type: str, name: str) -> None:
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type: str, name: str) -> None:
        self._on_lost(name)


@dataclass(frozen=True)
class Heard:
    """One datagram a scan received.

    @param over :data:`WS_DISCOVERY` or :data:`NETBIOS`, by the port it came
        in on.
    """

    over: str
    address: str
    data: bytes


class ScanChannel(Protocol):
    """What one scan sends and hears. Opened per scan and closed after it."""

    def ask_everyone(self, transaction_id: int) -> None:
        """Send the WS-Discovery probe and the NetBIOS name query."""

    def ask_host(self, address: str, transaction_id: int) -> None:
        """Send one host a node-status request."""

    def receive(self, timeout: float) -> Optional[Heard]:
        """The next datagram, or None when none came within ``timeout``."""

    def close(self) -> None: ...


class _UdpScan:
    """A scan's two sockets, each on a port of its own choosing: replies to a
    broadcast or a multicast come back to the port that asked.

    @note Owned by the one scan that opened it.
    """

    def __init__(self) -> None:
        self._netbios = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._wsd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            self._netbios.close()
            raise
        try:
            self._netbios.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._wsd.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        except OSError:
            self.close()
            raise

    def ask_everyone(self, transaction_id: int) -> None:
        query = name_query(transaction_id)
        for address in broadcast_addresses():
            self._send(self._netbios, query, address, _NBNS_PORT)
        probe = wsd_probe(f"urn:uuid:{uuid.uuid4()}")
        interfaces = multicast_interfaces()
        if not interfaces:
            self._send(self._wsd, probe, _WSD_GROUP, _WSD_PORT)
        for interface in interfaces:
            try:
                self._wsd.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(interface),
                )
            except OSError as exc:
                logger.debug("No WS-Discovery probe out of %s: %s", interface, exc)
                continue
            self._send(self._wsd, probe, _WSD_GROUP, _WSD_PORT)

    def ask_host(self, address: str, transaction_id: int) -> None:
        self._send(self._netbios, nbstat_query(transaction_id), address, _NBNS_PORT)

    def receive(self, timeout: float) -> Optional[Heard]:
        readable, _, _ = select.select([self._netbios, self._wsd], [], [], timeout)
        if not readable:
            return None
        sock = readable[0]
        data, (address, _port) = sock.recvfrom(65535)
        return Heard(WS_DISCOVERY if sock is self._wsd else NETBIOS, address, data)

    def close(self) -> None:
        self._netbios.close()
        self._wsd.close()

    @staticmethod
    def _send(sock: socket.socket, data: bytes, address: str, port: int) -> None:
        try:
            sock.sendto(data, (address, port))
        except OSError as exc:
            logger.debug("Nothing sent to %s:%d: %s", address, port, exc)


def _found_over(heard: Heard) -> Optional[str]:
    """How a datagram found a host, or None when it found none: a host is
    found by answering the probe as a computer, or the name query at all."""
    if heard.over == WS_DISCOVERY:
        return WS_DISCOVERY if is_computer_match(heard.data) else None
    return NETBIOS if answers_name_query(heard.data) else None


class SmbHostDiscovery:
    """The file servers this machine can see, kept up to date in the
    background.

    @note Owned by the process that serves the settings page. Starting a
        second one would put a second listener on the multicast group and
        a second scan on the network for the same answer.
    """

    def __init__(
        self,
        mdns_factory: MdnsFactory = _ZeroconfWatch,
        scan_factory: Callable[[], ScanChannel] = _UdpScan,
        scan_seconds: float = _SCAN_SECONDS,
        min_scan_interval: float = _MIN_SCAN_INTERVAL_S,
        stale_after: float = _STALE_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mdns_factory = mdns_factory
        self._scan_factory = scan_factory
        self._scan_seconds = scan_seconds
        self._min_scan_interval = min_scan_interval
        self._stale_after = stale_after
        self._now = now

        self._lock = threading.Lock()
        # Keyed by the announcement that carried them, so withdrawing one
        # takes its addresses with it.
        self._announced: dict[str, list[DiscoveredHost]] = {}
        # Keyed by address, with when it last answered: a reply to a scan
        # is a moment, not a subscription, so these have to expire.
        self._answered: dict[str, tuple[DiscoveredHost, float]] = {}

        self._mdns: Optional[MdnsWatch] = None
        self._warned_of_nonlocal_bind = False
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
            # and a lock left held would retire the scan for good.
            self._scanning.release()
            logger.warning("No thread to scan for shares on: %s", exc)

    def hosts(self) -> list[DiscoveredHost]:
        """Every server worth offering that was seen recently, by address.

        A host found more than one way is reported once, keeping whichever
        sighting carried a name, and a host with a routed IPv4 address is
        offered on IPv4 alone — the same shares under an IPv6 address are
        noise. This machine is left out on every address it holds — Samba
        answers on all of them, and the shares behind them are folders
        already offered as folders. Whose an address is gets asked here
        rather than on arrival, because it changes.
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
        ours_are_knowable = self._can_tell_whose_address_it_is()
        offered = [
            host
            for host in merged.values()
            if _is_a_server_address(host.address)
            and not (ours_are_knowable and _belongs_to_this_machine(host.address))
        ]
        return sorted(
            _without_ipv6_aliases(offered),
            key=lambda h: (h.name or h.address).casefold(),
        )

    def _can_tell_whose_address_it_is(self) -> bool:
        """Whether binding an address still says anything about who holds it.

        Where ``ip_nonlocal_bind`` is set every address binds, every server
        looks like this one and the list empties. One server too many beats
        none, so the rule stands down — saying so, because an empty list
        looks exactly like a quiet network.
        """
        if not _belongs_to_this_machine(_ASSIGNED_NOWHERE):
            return True
        if not self._warned_of_nonlocal_bind:
            self._warned_of_nonlocal_bind = True
            logger.warning(
                "This machine binds addresses it does not hold, so its own "
                "shares cannot be told from anyone else's and are offered too"
            )
        return False

    def _mdns_seen(self, key: str, name: str, addresses: list[str]) -> None:
        with self._lock:
            self._announced[key] = [
                DiscoveredHost(address=address, name=name, source="mDNS")
                for address in addresses
            ]

    def _mdns_lost(self, key: str) -> None:
        with self._lock:
            self._announced.pop(key, None)

    def _netbios_seen(self, address: str, name: str, source: str = NETBIOS) -> None:
        """A host that named its file server in a node-status reply.

        @param source How it was found before it was asked its name.
        """
        with self._lock:
            self._answered[address] = (
                DiscoveredHost(address=address, name=name, source=source),
                self._now(),
            )

    def _scan_and_release(self) -> None:
        try:
            self._scan_once()
        except OSError as exc:
            logger.debug("The scan for file servers stopped: %s", exc)
        finally:
            self._scanning.release()

    def _scan_once(self) -> None:
        """Ask who is out there, ask each host that answers for its name,
        and collect what comes back until the scan's time is up."""
        transaction_id = int(self._now()) & 0xFFFF
        deadline = time.monotonic() + self._scan_seconds
        channel = self._scan_factory()
        try:
            channel.ask_everyone(transaction_id)
            found_by: dict[str, str] = {}
            while not self._stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                heard = channel.receive(min(0.5, remaining))
                if heard is not None:
                    self._take(channel, heard, found_by, transaction_id)
        finally:
            channel.close()

    def _take(
        self,
        channel: ScanChannel,
        heard: Heard,
        found_by: dict[str, str],
        transaction_id: int,
    ) -> None:
        """Ask a host that has just been found for its name, once, or take
        the name a host asked earlier gives."""
        found = _found_over(heard)
        if found is not None:
            if heard.address not in found_by:
                found_by[heard.address] = found
                channel.ask_host(heard.address, transaction_id)
            return
        if heard.over != NETBIOS or heard.address not in found_by:
            return
        try:
            name = file_server_name(parse_nbstat_reply(heard.data))
        except ValueError as exc:
            logger.debug("Ignoring a reply from %s: %s", heard.address, exc)
            return
        if name is not None:
            self._netbios_seen(heard.address, name, found_by[heard.address])
