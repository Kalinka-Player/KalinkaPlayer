"""The socket under ``smbprotocol``, which no stream can talk into a huge buffer.

``smbprotocol`` sizes a frame from all 32 bits of its header and zero-fills a
buffer that big before the first byte of it arrives. When a receive times out
partway through a frame, what had arrived is dropped, and the next header is
read from the middle of the last payload: four bytes of audio taken for a
length of up to 4 GiB, allocated and filled at once.
"""

from __future__ import annotations

import errno
import logging
import select
import time

from smbprotocol.exceptions import SMBConnectionClosed
from smbprotocol.transport import Tcp

_HEADER_SIZE = 4
_SESSION_MESSAGE = 0x00
_SESSION_KEEPALIVE = 0x85
_PEER_GONE = (errno.ESHUTDOWN, errno.ECONNRESET, errno.ECONNABORTED)

logger = logging.getLogger(__name__.split(".")[-1])


class FramedTcp(Tcp):
    """Direct TCP transport that keeps its place in the stream.

    A header is checked before anything is sized from it. Its first byte is
    zero and the length is the other three (MS-SMB2 2.1), so no frame
    claims more than 16 MiB. Anything else means the stream is not SMB2 or
    has lost its place, and the connection is closed rather than read on.

    A receive that times out keeps what has arrived of the frame, and the
    next one carries on from there. ``smbprotocol`` answers the timeout with
    an echo and receives again, so a server that pauses mid-frame is waited
    for exactly as one that pauses between frames.

    @note Receives come from one thread, the connection's worker, as with
        the class this replaces.
    """

    def __init__(self, server, port, timeout=None):
        super().__init__(server, port, timeout)
        self._expect_header()

    def connect(self) -> None:
        super().connect()
        self._expect_header()

    def recv(self, timeout: float) -> bytes:
        """The next SMB2 message, or ``b""`` once the socket has closed.

        @throws TimeoutError when the server says nothing for ``timeout``
            seconds; the frame in progress is kept for the next call.
        @throws SMBConnectionClosed when a header is not one an SMB2 server
            sends; the connection is closed by then.
        """
        while True:
            filled, timeout = self._fill(timeout)
            if not filled:
                return b""
            if self._in_payload:
                message = bytes(self._frame)
                self._expect_header()
                return message
            length = self._payload_length()
            if length:
                self._frame = bytearray(length)
                self._filled = 0
                self._in_payload = True
            else:
                self._expect_header()

    def _expect_header(self) -> None:
        self._frame = bytearray(_HEADER_SIZE)
        self._filled = 0
        self._in_payload = False

    def _payload_length(self) -> int:
        """The length a header announces. A NetBIOS keepalive (RFC 1002
        4.3.7) announces nothing and is skipped like an empty message."""
        kind = self._frame[0]
        length = int.from_bytes(self._frame[1:], "big")
        if kind == _SESSION_MESSAGE or (kind == _SESSION_KEEPALIVE and not length):
            return length
        self.close()
        raise SMBConnectionClosed(
            f"The server sent {self._frame.hex(' ')} where an SMB2 frame "
            f"header belongs; the connection is closed"
        )

    def _fill(self, timeout: float) -> tuple[bool, float]:
        """Receive until the frame buffer is full.

        @return Whether it filled, False once the socket has closed, and
            what remains of ``timeout``.
        """
        view = memoryview(self._frame)
        while self._filled < len(self._frame):
            start = time.monotonic()
            with self._close_lock:
                if not self.connected:
                    return False, timeout
                ready = select.select([self._sock], [], [], max(timeout, 1))[0]
                idle = time.monotonic() - start
                timeout -= idle
                if not ready:
                    raise TimeoutError(
                        f"recv idle for {idle:.1f}s "
                        f"({self._filled}/{len(self._frame)} bytes)"
                    )
                try:
                    received = self._sock.recv_into(view[self._filled :])
                except OSError as e:
                    if e.errno not in _PEER_GONE:
                        raise
                    logger.warning(
                        "SMB socket aborted by peer (%s), treating as closed",
                        errno.errorcode.get(e.errno, e.errno),
                    )
                    received = 0
            if not received:
                self.close()
                return False, timeout
            self._filled += received
        return True, timeout
