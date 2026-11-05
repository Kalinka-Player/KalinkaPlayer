"""Finding NFS exports: the two RPC calls, and the search around them."""

import socket
import struct
import threading
import time

import pytest

from kalinka_server.storage.nfs_discovery import (
    PROBE_WORKERS,
    Announcement,
    NfsDiscovery,
    NoMountDaemon,
    RpcError,
    list_exports,
)


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack(">I", len(data)) + data + b"\0" * (-len(data) % 4)


def _reply(xid: int, results: bytes) -> bytes:
    """An accepted, successful reply, framed as one record."""
    message = struct.pack(">IIIIII", xid, 1, 0, 0, 0, 0) + results
    return struct.pack(">I", 0x80000000 | len(message)) + message


def _exports(*entries) -> bytes:
    body = b""
    for path, clients in entries:
        body += struct.pack(">I", 1) + _string(path)
        for client in clients:
            body += struct.pack(">I", 1) + _string(client)
        body += struct.pack(">I", 0)
    return body + struct.pack(">I", 0)


class _Socket:
    """One connection to a fake host: what was sent, and its answer to it."""

    def __init__(self, answer):
        self._answer = answer
        self.sent = b""
        self._pending = b""
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.sent += data
        (xid,) = struct.unpack_from(">I", data, 4)
        self._pending = self._answer(xid)

    def recv(self, count):
        chunk, self._pending = self._pending[:count], self._pending[count:]
        return chunk

    def close(self):
        self.closed = True


class _Host:
    """rpcbind on 111 and a mount daemon on the port it names."""

    def __init__(self, port=20048, exports=b""):
        self.port = port
        self.exports = exports
        self.connections = []

    def connect(self, address, _timeout):
        host, port = address
        if port == 111:
            sock = _Socket(lambda xid: _reply(xid, struct.pack(">I", self.port)))
        elif port == self.port:
            sock = _Socket(lambda xid: _reply(xid, self.exports))
        else:
            raise ConnectionRefusedError(port)
        self.connections.append((address, sock))
        return sock


def test_rpcbind_is_asked_for_the_mount_daemon_over_tcp():
    host = _Host(exports=_exports())

    list_exports("nas.local", connect=host.connect)

    (address, sock), _mountd = host.connections
    assert address == ("nas.local", 111)
    (xid,) = struct.unpack_from(">I", sock.sent, 4)
    assert sock.sent == struct.pack(
        ">" + "I" * 15,
        0x80000000 | 56,
        xid,
        0,  # a call
        2,  # of RPC version 2
        100000,  # to rpcbind
        2,
        3,  # GETPORT
        0, 0,  # no credentials
        0, 0,  # no verifier
        100005,  # for the mount daemon
        3,
        6,  # over TCP
        0,
    )
    assert sock.closed


def test_the_mount_daemon_is_asked_for_its_exports():
    host = _Host(exports=_exports())

    list_exports("nas.local", connect=host.connect)

    _rpcbind, (address, sock) = host.connections
    assert address == ("nas.local", 20048)
    (xid,) = struct.unpack_from(">I", sock.sent, 4)
    assert sock.sent == struct.pack(">IIIIIIIIIII", 0x80000000 | 40, xid, 0, 2, 100005, 3, 5, 0, 0, 0, 0)


def test_each_export_comes_with_the_clients_it_allows():
    host = _Host(exports=_exports(("/volume1/music", ["192.168.1.0/24", "kalinka"]), ("/srv/flac", [])))

    assert list_exports("nas.local", connect=host.connect) == [
        ("/volume1/music", ("192.168.1.0/24", "kalinka")),
        ("/srv/flac", ()),
    ]


def test_an_answer_in_several_fragments_is_read_whole():
    def split(xid):
        whole = _reply(xid, _exports(("/music", [])))[4:]
        first, rest = whole[:10], whole[10:]
        return struct.pack(">I", len(first)) + first + struct.pack(">I", 0x80000000 | len(rest)) + rest

    host = _Host()
    connect = host.connect

    def connect_split(address, timeout):
        sock = connect(address, timeout)
        if address[1] == host.port:
            sock._answer = split
        return sock

    assert list_exports("nas.local", connect=connect_split) == [("/music", ())]


def test_a_host_whose_rpcbind_knows_no_mount_daemon_lists_nothing():
    """As an NFSv4-only server answers."""
    with pytest.raises(NoMountDaemon):
        list_exports("nas.local", connect=_Host(port=0).connect)


def test_an_answer_to_another_call_is_refused():
    host = _Host()

    def connect(address, timeout):
        return _Socket(lambda xid: _reply(xid + 1, struct.pack(">I", 20048)))

    with pytest.raises(RpcError):
        list_exports("nas.local", connect=connect)


def test_a_host_that_never_answers_times_out():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        started = time.monotonic()
        with pytest.raises(OSError):
            list_exports(
                "127.0.0.1",
                connect=lambda _address, timeout: socket.create_connection(("127.0.0.1", port), timeout),
                timeout=0.2,
            )
        assert time.monotonic() - started < 2
    finally:
        listener.close()


class _Watch:
    def __init__(self, on_seen, on_lost, announcements):
        self._on_seen = on_seen
        self._announcements = announcements
        self.running = False

    def start(self):
        self.running = True
        for key, announcement in self._announcements.items():
            self._on_seen(key, announcement)

    def stop(self):
        self.running = False


class _Network:
    """What a discovery sees: announcements, and each host's answer."""

    def __init__(self, announcements=(), answers=None):
        self.announcements = dict(announcements)
        self.answers = answers or {}
        self.asked = []
        self.watches = []

    def watch(self, on_seen, on_lost):
        watch = _Watch(on_seen, on_lost, self.announcements)
        self.watches.append(watch)
        return watch

    def ask(self, host):
        self.asked.append(host)
        answer = self.answers.get(host)
        if answer is None:
            raise TimeoutError(host)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _discovery(network, **kwargs):
    return NfsDiscovery(
        mdns_factory=network.watch,
        ask=network.ask,
        resolves=lambda name: name.endswith(".local"),
        settle=0,
        tick=0.01,
        **kwargs,
    )


def _wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _searched(discovery):
    discovery.exports()
    _wait_for(lambda: not discovery.scan_state().running)
    return discovery.exports()


DS920 = Announcement(server="ds920.local", instance="DS920", addresses=("192.168.1.20", "fe80::1"))


def test_a_host_announcing_smb_is_asked_for_its_exports():
    network = _Network(
        {"DS920._smb._tcp.local.": DS920},
        {"ds920.local": [("/volume1/music", ("192.168.1.0/24",))]},
    )
    discovery = _discovery(network)
    try:
        (export,) = _searched(discovery)
    finally:
        discovery.close()

    assert (export.host, export.path, export.server, export.clients) == (
        "ds920.local", "/volume1/music", "DS920", ("192.168.1.0/24",),
    )
    assert export.is_on("192.168.1.20") and not export.is_on("fe80::1")
    assert discovery.scan_state().finished_at is not None


def test_a_host_whose_name_does_not_resolve_here_is_mounted_by_its_address():
    network = _Network(
        {"DS920._smb._tcp.local.": Announcement(server="ds920.lan", instance="DS920", addresses=("192.168.1.20",))},
        {"192.168.1.20": [("/volume1/music", ())]},
    )
    discovery = _discovery(network)
    try:
        (export,) = _searched(discovery)
    finally:
        discovery.close()

    assert export.host == "192.168.1.20"


def test_the_export_an_nfs_announcement_names_is_found_though_the_host_lists_none():
    network = _Network(
        {
            "DS920._nfs._tcp.local.": Announcement(
                server="ds920.local", instance="DS920", addresses=("192.168.1.20",), path="/music"
            ),
            "DS920._smb._tcp.local.": DS920,
        },
        {"ds920.local": NoMountDaemon("ds920.local")},
    )
    discovery = _discovery(network)
    try:
        (export,) = _searched(discovery)
    finally:
        discovery.close()

    assert (export.host, export.path) == ("ds920.local", "/music")
    # Both announcements are one host, asked once.
    assert network.asked == ["ds920.local"]


def test_the_hosts_of_saved_shares_are_asked_too():
    network = _Network(answers={"10.0.0.5": [("/flac", ())]})
    discovery = _discovery(network, hosts=lambda: ["10.0.0.5"])
    try:
        assert [(e.host, e.path) for e in _searched(discovery)] == [("10.0.0.5", "/flac")]
    finally:
        discovery.close()


def test_a_host_typed_in_is_answered_at_once_and_asked_again_with_the_others():
    network = _Network(answers={"nas": [("/music", ())], "nfs4only": NoMountDaemon("nfs4only")})
    discovery = _discovery(network)
    try:
        answer = discovery.probe(" nas ")
        quiet = discovery.probe("nfs4only")
        dead = discovery.probe("gone")
        found = _searched(discovery)
    finally:
        discovery.close()

    assert answer.reachable and [e.path for e in answer.exports] == ["/music"]
    assert (quiet.reachable, quiet.exports, quiet.error) == (True, (), "This host lists no exports")
    assert (dead.reachable, dead.error) == (False, "No answer from this host")
    assert [(e.host, e.path) for e in found] == [("nas", "/music")]


def test_one_host_failing_oddly_does_not_end_the_search():
    network = _Network(answers={"a": ValueError("odd"), "b": [("/music", ())]})
    discovery = _discovery(network, hosts=lambda: ["a", "b"])
    try:
        assert [e.host for e in _searched(discovery)] == ["b"]
    finally:
        discovery.close()


def test_nothing_is_looked_for_until_someone_looks_and_the_search_stops_after():
    network = _Network({"DS920._smb._tcp.local.": DS920}, {"ds920.local": []})
    discovery = _discovery(network, idle_after=0.1)
    try:
        time.sleep(0.05)
        assert network.watches == [] and network.asked == []

        discovery.exports()
        _wait_for(lambda: network.asked)
        assert network.watches[0].running

        _wait_for(lambda: not network.watches[0].running)
        asked = len(network.asked)
        time.sleep(0.05)
        assert len(network.asked) == asked

        discovery.exports()
        _wait_for(lambda: len(network.watches) == 2 and network.watches[1].running)
    finally:
        discovery.close()


def test_a_host_left_unasked_when_the_search_stopped_is_asked_by_the_next():
    release = threading.Event()
    asked = []

    def slow(host):
        asked.append(host)
        release.wait(2)
        return [("/music", ())]

    hosts = [f"nas{n}" for n in range(PROBE_WORKERS + 2)]
    discovery = NfsDiscovery(
        hosts=lambda: hosts, mdns_factory=_Network().watch, ask=slow, settle=0, tick=0.01, idle_after=0.1
    )
    try:
        discovery.exports()
        _wait_for(lambda: len(asked) == PROBE_WORKERS)
        time.sleep(0.2)
        release.set()

        discovery.exports()
        _wait_for(lambda: set(asked) == set(hosts))
        _wait_for(lambda: not discovery.scan_state().running)
    finally:
        discovery.close()


def test_the_search_runs_until_the_hosts_have_answered():
    answered = threading.Event()

    def slow(host):
        answered.wait(2)
        return [("/music", ())]

    discovery = NfsDiscovery(
        hosts=lambda: ["nas"], mdns_factory=_Network().watch, ask=slow, settle=0, tick=0.01
    )
    try:
        discovery.exports()
        _wait_for(lambda: discovery.scan_state().running)
        time.sleep(0.05)
        assert discovery.scan_state().running
        answered.set()
        _wait_for(lambda: not discovery.scan_state().running)
    finally:
        discovery.close()


def test_a_rescan_asks_every_host_again_and_keeps_what_they_said_meanwhile():
    network = _Network(answers={"nas": [("/music", ())]})
    discovery = _discovery(network, hosts=lambda: ["nas"])
    try:
        assert [e.path for e in _searched(discovery)] == ["/music"]
        network.answers["nas"] = [("/music", ()), ("/video", ())]

        discovery.rescan()

        assert discovery.scan_state().running
        assert [e.path for e in discovery.exports()] == ["/music"]
        assert [e.path for e in _searched(discovery)] == ["/music", "/video"]
        assert network.asked == ["nas", "nas"]
    finally:
        discovery.close()
