#!/usr/bin/env python3
"""Receiving SMB2 frames off a stream that may be broken.

What the transport owes a connection: no buffer sized from anything but a
well-formed header, and no frame lost to a timeout, which would leave the
next header to be read from the middle of a payload.
"""

import socket
import struct
import threading
import time
import tracemalloc
import uuid

import pytest
from smbprotocol.connection import Connection
from smbprotocol.exceptions import SMBConnectionClosed

import kalinka_plugin_localfiles.storage.smb  # noqa: F401  installs FramedTcp
from kalinka_plugin_localfiles.storage.smb_transport import FramedTcp

_MiB = 1024 * 1024


def frame(payload: bytes) -> bytes:
    return struct.pack(">L", len(payload)) + payload


def send_while_receiving(peer: socket.socket, data: bytes) -> threading.Thread:
    """A loopback buffer may hold less than ``data``, so a send that waits
    for the transport to read must not block the test that reads."""
    sender = threading.Thread(target=peer.sendall, args=(data,), daemon=True)
    sender.start()
    return sender


@pytest.fixture
def link():
    """A transport connected to a socket the test speaks for the server."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        transport = FramedTcp("127.0.0.1", listener.getsockname()[1], timeout=5)
        transport.connect()
        peer, _ = listener.accept()
    yield transport, peer
    transport.close()
    peer.close()


@pytest.fixture
def allocations():
    tracemalloc.start()
    yield lambda: tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()


class TestFrames:
    def test_messages_arrive_one_frame_each(self, link):
        transport, peer = link
        peer.sendall(frame(b"first") + frame(b"second"))

        assert transport.recv(5) == b"first"
        assert transport.recv(5) == b"second"

    def test_a_netbios_keepalive_is_skipped(self, link):
        transport, peer = link
        peer.sendall(b"\x85\x00\x00\x00" + frame(b"message"))

        assert transport.recv(5) == b"message"

    def test_a_server_closing_mid_frame_reads_as_closed(self, link):
        transport, peer = link
        peer.sendall(frame(b"x" * 100)[:50])
        peer.close()

        assert transport.recv(5) == b""
        assert not transport.connected


class TestATimeoutMidFrame:
    @pytest.mark.parametrize("cut", [2, 4 + 512 * 1024], ids=["header", "payload"])
    def test_the_next_receive_finishes_the_frame(self, link, cut):
        """The bytes either side of the cut read as a 2 GiB length to a
        transport that lost its place."""
        transport, peer = link
        payload = bytearray(_MiB)
        payload[512 * 1024 : 512 * 1024 + 4] = b"\x85\x00\x00\x00"
        sent = frame(bytes(payload))
        head = send_while_receiving(peer, sent[:cut])

        with pytest.raises(TimeoutError):
            transport.recv(0)
        head.join(5)
        tail = send_while_receiving(peer, sent[cut:] + frame(b"next"))

        assert transport.recv(5) == payload
        assert transport.recv(5) == b"next"
        tail.join(5)


class TestABrokenStream:
    def test_a_header_no_smb2_server_sends_closes_the_connection(
        self, link, allocations
    ):
        transport, peer = link
        peer.sendall(b"\xff\xff\xff\xff")

        with pytest.raises(SMBConnectionClosed, match="ff ff ff ff"):
            transport.recv(5)

        assert not transport.connected
        assert allocations() < _MiB

    def test_a_connection_drops_a_server_that_announces_gigabytes(self, allocations):
        """Through ``smbprotocol``'s own connection, which must be building
        its socket from the guarded transport."""
        with socket.create_server(("127.0.0.1", 0)) as listener:

            def answer_the_negotiation_with_a_huge_frame():
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(5)
                    peer.recv(65536)
                    peer.sendall(b"\xff\xff\xff\xf0")
                    peer.recv(1)

            server = threading.Thread(
                target=answer_the_negotiation_with_a_huge_frame, daemon=True
            )
            server.start()
            connection = Connection(
                uuid.uuid4(), "127.0.0.1", listener.getsockname()[1]
            )
            started = time.monotonic()

            with pytest.raises(SMBConnectionClosed):
                connection.connect(timeout=5)

            server.join(5)
        assert time.monotonic() - started < 3
        assert allocations() < 16 * _MiB
