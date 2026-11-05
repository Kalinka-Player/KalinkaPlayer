"""Finding the NFS exports on this network, the way ``showmount -e`` does.

Candidates are the hosts that announce ``_nfs._tcp`` or ``_smb._tcp`` over
mDNS, as a NAS that serves both often announces only SMB, the hosts of saved
shares and the hosts someone typed in. Each is asked rpcbind for its mount
daemon and the mount daemon for its exports, two small ONC RPC calls over TCP
that need no privilege.

Nothing is looked for unless someone is looking: the first request starts
the mDNS watch and a thread that asks the candidates again every so often,
and both stop once requests stop coming.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Protocol

logger = logging.getLogger(__name__.split(".")[-1])

MDNS_NFS = "_nfs._tcp.local."
MDNS_SMB = "_smb._tcp.local."

RPCBIND_PORT = 111
_PMAP_PROGRAM, _PMAP_VERSION, _PMAP_GETPORT = 100000, 2, 3
_MOUNT_PROGRAM, _MOUNT_VERSION, _MOUNT_EXPORT = 100005, 3, 5
_IPPROTO_TCP = 6

_CALL, _REPLY = 0, 1
_RPC_VERSION = 2
_AUTH_NONE = 0
_MSG_ACCEPTED = 0
_SUCCESS = 0
_LAST_FRAGMENT = 0x80000000

#: Longer than any exports list a NAS sends, short of what would hurt.
_MAX_REPLY_BYTES = 1 << 20
_MAX_PATH_BYTES = 1024
_MAX_GROUP_BYTES = 255

#: How long one host is given to answer each of the two calls.
PROBE_TIMEOUT_S = 2.0
#: How many hosts are asked at once.
PROBE_WORKERS = 4
#: How often a host is asked again while someone looks.
SCAN_INTERVAL_S = 30.0
#: How long the watch keeps running after the last request.
IDLE_AFTER_S = 60.0
#: How long announcements are given to arrive before a search can be over.
SETTLE_S = 3.0
_TICK_S = 1.0


class RpcError(Exception):
    """A host answered, but not with what was asked for."""


class NoMountDaemon(Exception):
    """The host's rpcbind knows no mount daemon, as on an NFSv4-only server."""


def rpc_call(xid: int, program: int, version: int, procedure: int, args: bytes = b"") -> bytes:
    """A call with no credentials, framed as one record for TCP."""
    message = struct.pack(
        ">IIIIIIIIII",
        xid,
        _CALL,
        _RPC_VERSION,
        program,
        version,
        procedure,
        _AUTH_NONE,
        0,
        _AUTH_NONE,
        0,
    ) + args
    return struct.pack(">I", _LAST_FRAGMENT | len(message)) + message


def getport_args(program: int, version: int) -> bytes:
    return struct.pack(">IIII", program, version, _IPPROTO_TCP, 0)


class _Xdr:
    def __init__(self, data: bytes):
        self._data = data
        self._at = 0

    def uint(self) -> int:
        if self._at + 4 > len(self._data):
            raise RpcError("the answer ends early")
        (value,) = struct.unpack_from(">I", self._data, self._at)
        self._at += 4
        return value

    def opaque(self, limit: int) -> bytes:
        length = self.uint()
        if length > limit or self._at + length > len(self._data):
            raise RpcError("a field in the answer is too long")
        value = self._data[self._at : self._at + length]
        self._at += (length + 3) & ~3
        return value

    def rest(self) -> bytes:
        return self._data[self._at :]


def reply_body(message: bytes, xid: int) -> bytes:
    """The results of a successful reply to call ``xid``."""
    reader = _Xdr(message)
    if reader.uint() != xid or reader.uint() != _REPLY:
        raise RpcError("not the answer to this call")
    if reader.uint() != _MSG_ACCEPTED:
        raise RpcError("the call was refused")
    reader.uint()
    reader.opaque(400)
    status = reader.uint()
    if status != _SUCCESS:
        raise RpcError(f"the call failed with status {status}")
    return reader.rest()


def parse_exports(body: bytes) -> list[tuple[str, tuple[str, ...]]]:
    """The exports a mount daemon listed, each with the clients it allows."""
    reader = _Xdr(body)
    found = []
    while reader.uint():
        path = reader.opaque(_MAX_PATH_BYTES)
        clients = []
        while reader.uint():
            clients.append(reader.opaque(_MAX_GROUP_BYTES))
        try:
            found.append((path.decode(), tuple(c.decode() for c in clients)))
        except UnicodeDecodeError:
            logger.debug("Leaving out an export whose path is not UTF-8")
    return found


Connect = Callable[[tuple[str, int], float], socket.socket]


def _connect(address: tuple[str, int], timeout: float) -> socket.socket:
    return socket.create_connection(address, timeout=timeout)


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    chunks, left = [], count
    while left:
        chunk = sock.recv(min(left, 65536))
        if not chunk:
            raise RpcError("the host closed the connection")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _read_record(sock: socket.socket) -> bytes:
    fragments, total = [], 0
    while True:
        (header,) = struct.unpack(">I", _read_exactly(sock, 4))
        length = header & ~_LAST_FRAGMENT
        total += length
        if total > _MAX_REPLY_BYTES:
            raise RpcError("the answer is too long")
        fragments.append(_read_exactly(sock, length))
        if header & _LAST_FRAGMENT:
            return b"".join(fragments)


def _call(connect: Connect, host: str, port: int, call: bytes, xid: int, timeout: float) -> bytes:
    sock = connect((host, port), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(call)
        return reply_body(_read_record(sock), xid)
    finally:
        sock.close()


def list_exports(
    host: str, connect: Connect = _connect, timeout: float = PROBE_TIMEOUT_S
) -> list[tuple[str, tuple[str, ...]]]:
    """Ask ``host`` for its exports.

    @throws OSError When the host does not answer.
    @throws NoMountDaemon When it answers but lists no mount daemon.
    @throws RpcError When it answers something else.
    """
    xid = time.monotonic_ns() & 0xFFFFFFFF
    asked = rpc_call(xid, _PMAP_PROGRAM, _PMAP_VERSION, _PMAP_GETPORT, getport_args(_MOUNT_PROGRAM, _MOUNT_VERSION))
    port = _Xdr(_call(connect, host, RPCBIND_PORT, asked, xid, timeout)).uint()
    if not 0 < port < 65536:
        raise NoMountDaemon(host)
    xid = (xid + 1) & 0xFFFFFFFF
    asked = rpc_call(xid, _MOUNT_PROGRAM, _MOUNT_VERSION, _MOUNT_EXPORT)
    return parse_exports(_call(connect, host, port, asked, xid, timeout))


@dataclass(frozen=True)
class Export:
    """An export a host offers.

    @param host What a mount names the host by: a name this machine can
        look up, or else its address.
    @param server What the host calls itself, empty when nothing said.
    @param clients Who the export lets in, as its mount daemon says; empty
        when only an mDNS announcement named it.
    @param aliases Every name and address the host was seen by, which a
        saved share may name it by.
    """

    host: str
    path: str
    server: str = ""
    clients: tuple[str, ...] = ()
    aliases: frozenset[str] = frozenset()

    def is_on(self, host: str) -> bool:
        return host == self.host or host in self.aliases


@dataclass(frozen=True)
class HostAnswer:
    """What one host said when asked for its exports.

    @param reachable Whether it answered at all.
    @param error Why it listed nothing, for a person to read.
    """

    host: str
    reachable: bool
    exports: tuple[Export, ...] = ()
    error: Optional[str] = None


@dataclass(frozen=True)
class ScanState:
    """@param finished_at When the hosts last finished answering, in Unix time."""

    running: bool
    finished_at: Optional[float]


@dataclass(frozen=True)
class Announcement:
    """A host announcing a file service over mDNS.

    @param path The export an ``_nfs._tcp`` announcement names in its TXT
        record, empty for one that names none.
    """

    server: str
    instance: str
    addresses: tuple[str, ...]
    path: str = ""


class MdnsWatch(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


MdnsFactory = Callable[[Callable[[str, Announcement], None], Callable[[str], None]], MdnsWatch]


class _ZeroconfWatch:
    """The ``_nfs._tcp`` and ``_smb._tcp`` announcements, through zeroconf.

    @note Its callbacks arrive on zeroconf's own thread, which may block to
        resolve a service.
    """

    def __init__(self, on_seen: Callable[[str, Announcement], None], on_lost: Callable[[str], None]):
        self._on_seen = on_seen
        self._on_lost = on_lost
        self._zeroconf = None
        self._browser = None

    def start(self) -> None:
        from zeroconf import ServiceBrowser, Zeroconf

        self._zeroconf = Zeroconf()
        try:
            self._browser = ServiceBrowser(self._zeroconf, [MDNS_NFS, MDNS_SMB], self)
        except Exception:
            # The listener holds the multicast sockets already.
            self.stop()
            raise

    def stop(self) -> None:
        # The browser first: it is a thread working on the listener's sockets.
        for part, release in ((self._browser, "cancel"), (self._zeroconf, "close")):
            if part is None:
                continue
            try:
                getattr(part, release)()
            except Exception as exc:  # noqa: BLE001 — teardown, never fatal
                logger.warning("Stopping the mDNS browser failed: %s", exc)
        self._browser = None
        self._zeroconf = None

    def add_service(self, zeroconf, service_type: str, name: str) -> None:
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
        except Exception as exc:  # noqa: BLE001 — raising here ends the browser
            logger.warning("Could not resolve %s over mDNS: %r", name, exc)
            return
        if info is None:
            return
        path = b""
        if service_type == MDNS_NFS:
            path = (info.properties or {}).get(b"path") or b""
        self._on_seen(
            name,
            Announcement(
                server=(info.server or "").rstrip("."),
                instance=name.removesuffix(f".{service_type}"),
                addresses=tuple(info.parsed_addresses()),
                path=path.decode(errors="replace"),
            ),
        )

    def update_service(self, zeroconf, service_type: str, name: str) -> None:
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type: str, name: str) -> None:
        self._on_lost(name)


def _is_ipv4(address: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
    except ValueError:
        return False


def _resolves(name: str) -> bool:
    try:
        return bool(socket.getaddrinfo(name, None))
    except OSError:
        return False


@dataclass
class _Candidate:
    host: str
    server: str = ""
    aliases: set[str] = field(default_factory=set)
    announced: set[str] = field(default_factory=set)


class NfsDiscovery:
    """The NFS exports this machine can see, looked for while someone looks.

    @param hosts The hosts of the saved shares, asked as well.
    @note Thread-safe. Every call returns at once but :meth:`probe`, which
        asks a host and waits for it: call that off the event loop.
    """

    def __init__(
        self,
        hosts: Callable[[], Iterable[str]] = lambda: (),
        *,
        mdns_factory: MdnsFactory = _ZeroconfWatch,
        ask: Callable[[str], list[tuple[str, tuple[str, ...]]]] = list_exports,
        resolves: Callable[[str], bool] = _resolves,
        scan_interval: float = SCAN_INTERVAL_S,
        idle_after: float = IDLE_AFTER_S,
        settle: float = SETTLE_S,
        tick: float = _TICK_S,
        now: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._hosts = hosts
        self._mdns_factory = mdns_factory
        self._ask = ask
        self._resolves = resolves
        self._scan_interval = scan_interval
        self._idle_after = idle_after
        self._settle = settle
        self._tick = tick
        self._now = now
        self._wall_clock = wall_clock

        self._lock = threading.Lock()
        self._announced: dict[str, Announcement] = {}
        self._typed: set[str] = set()
        self._answers: dict[str, tuple[HostAnswer, float]] = {}
        self._asking: set[str] = set()
        self._answered_since = False
        self._finished_at: Optional[float] = None
        self._looked_at = 0.0
        self._worker: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def exports(self) -> list[Export]:
        """What the hosts said last, keeping the search going a while longer."""
        self._keep_looking()
        with self._lock:
            candidates = self._candidates()
            answers = dict(self._answers)
        found: dict[tuple[str, str], Export] = {}
        for candidate in candidates:
            answer = answers.get(candidate.host)
            for export in answer[0].exports if answer else ():
                found[(export.host, export.path)] = self._export(candidate, export.path, export.clients)
            for path in candidate.announced:
                found.setdefault((candidate.host, path), self._export(candidate, path))
        return sorted(found.values(), key=lambda e: ((e.server or e.host).casefold(), e.path))

    def scan_state(self) -> ScanState:
        with self._lock:
            return ScanState(running=bool(self._asking) or self._finished_at is None, finished_at=self._finished_at)

    def probe(self, host: str) -> HostAnswer:
        """Ask ``host`` now, and keep asking it with the others."""
        host = host.strip()
        with self._lock:
            self._typed.add(host)
            candidate = next((c for c in self._candidates() if c.host == host or host in c.aliases), None)
        answer = self._probe(candidate or _Candidate(host=host, aliases={host}))
        with self._lock:
            self._answers[answer.host] = (answer, self._now())
        return answer

    def rescan(self) -> None:
        """Ask every host again now. What they said last is listed until
        they answer, and the search runs until then."""
        with self._lock:
            asked_long_ago = self._now() - self._scan_interval
            self._answers = {host: (answer, asked_long_ago) for host, (answer, _) in self._answers.items()}
            self._finished_at = None
        self._keep_looking()

    def close(self) -> None:
        """Stop looking. Returns at once; a host being asked has the rest
        of its timeout to answer."""
        self._stopping.set()

    def _keep_looking(self) -> None:
        with self._lock:
            self._looked_at = self._now()
            if self._worker is not None or self._stopping.is_set():
                return
            self._worker = threading.Thread(target=self._look, name="nfs-discovery", daemon=True)
        try:
            self._worker.start()
        except RuntimeError as exc:
            with self._lock:
                self._worker = None
            logger.warning("No thread to look for NFS shares on: %s", exc)

    def _look(self) -> None:
        with self._lock:
            self._announced.clear()
            self._finished_at = None
        started = self._now()
        watch = self._start_watch()
        pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="nfs-probe")
        try:
            while not self._stopping.is_set():
                with self._lock:
                    if self._now() - self._looked_at >= self._idle_after:
                        # A request from now on starts another search.
                        self._worker = None
                        return
                    due = [c for c in self._candidates() if self._due(c.host)]
                    self._asking.update(c.host for c in due)
                    settled = self._now() - started >= self._settle
                    if not due and not self._asking and settled and (self._finished_at is None or self._answered_since):
                        self._finished_at = self._wall_clock()
                        self._answered_since = False
                for candidate in due:
                    pool.submit(self._probe_and_keep, candidate)
                self._stopping.wait(self._tick)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                # A probe cancelled before it ran would otherwise stay asked forever.
                self._asking.clear()
            if watch is not None:
                watch.stop()

    def _start_watch(self) -> Optional[MdnsWatch]:
        try:
            watch = self._mdns_factory(self._seen, self._lost)
            watch.start()
            return watch
        except Exception as exc:  # noqa: BLE001 — saved and typed hosts are still asked
            logger.warning("Shares announced over mDNS will not be found: %s", exc)
            return None

    def _due(self, host: str) -> bool:
        if host in self._asking:
            return False
        answered = self._answers.get(host)
        return answered is None or self._now() - answered[1] >= self._scan_interval

    def _probe_and_keep(self, candidate: _Candidate) -> None:
        try:
            answer = self._probe(candidate)
        except Exception as exc:  # noqa: BLE001 — one host must not end the search
            logger.warning("Asking %s for its exports failed: %r", candidate.host, exc)
            answer = HostAnswer(host=candidate.host, reachable=False, error="Could not ask this host")
        with self._lock:
            self._answers[candidate.host] = (answer, self._now())
            self._asking.discard(candidate.host)
            self._answered_since = True

    def _probe(self, candidate: _Candidate) -> HostAnswer:
        try:
            listed = self._ask(candidate.host)
        except NoMountDaemon:
            return HostAnswer(host=candidate.host, reachable=True, error="This host lists no exports")
        except RpcError as exc:
            return HostAnswer(host=candidate.host, reachable=True, error=f"This host answered oddly: {exc}")
        except OSError:
            return HostAnswer(host=candidate.host, reachable=False, error="No answer from this host")
        exports = tuple(self._export(candidate, path, clients) for path, clients in listed)
        return HostAnswer(
            host=candidate.host,
            reachable=True,
            exports=exports,
            error=None if exports else "This host lists no exports",
        )

    @staticmethod
    def _export(candidate: _Candidate, path: str, clients: tuple[str, ...] = ()) -> Export:
        return Export(
            host=candidate.host,
            path=path,
            server=candidate.server,
            clients=clients,
            aliases=frozenset(candidate.aliases),
        )

    def _seen(self, key: str, announcement: Announcement) -> None:
        # Looked up here, on zeroconf's thread, rather than under the lock.
        named = announcement.server if announcement.server and self._resolves(announcement.server) else ""
        with self._lock:
            self._announced[key] = Announcement(
                server=named,
                instance=announcement.instance,
                addresses=announcement.addresses,
                path=announcement.path,
            )

    def _lost(self, key: str) -> None:
        with self._lock:
            self._announced.pop(key, None)

    def _candidates(self) -> list[_Candidate]:
        """Every host to ask, one entry however many ways it was seen.

        @note Called with the lock held.
        """
        by_host: dict[str, _Candidate] = {}
        alias_of: dict[str, str] = {}

        def add(host: str, server: str = "", aliases: Iterable[str] = (), path: str = "") -> None:
            host = alias_of.get(host, host)
            candidate = by_host.setdefault(host, _Candidate(host=host))
            candidate.server = candidate.server or server
            candidate.aliases.update({host, *aliases})
            for alias in candidate.aliases:
                alias_of.setdefault(alias, host)
            if path:
                candidate.announced.add(path)

        for announcement in self._announced.values():
            ipv4 = [a for a in announcement.addresses if _is_ipv4(a)]
            host = announcement.server or (ipv4[0] if ipv4 else "")
            if host:
                add(host, announcement.instance, ipv4, announcement.path)
        for host in [*self._hosts(), *self._typed]:
            if host:
                add(host)
        return list(by_host.values())
