#!/usr/bin/env python3
"""Byte ranges, and who closes the stream.

The endpoint's own tests drive it through a client, which can neither hang up
mid-track nor cancel a request while the storage is still opening. Both are
ordinary events — a renderer abandons a range request on every seek, and the
server cancels in flight on shutdown — and both used to leak the handle,
which on a share is a handle the server holds open until it exits. So they
are exercised here against the pieces directly.
"""

import asyncio
import io

import pytest

from kalinka_server.ranged_content import (
    UnsatisfiableRange,
    head_response,
    open_reader,
    resolve_range,
    stream_response,
)

AUDIO = bytes(range(256)) * 8  # 2048 bytes


class _Handle(io.BytesIO):
    """A stream that remembers being closed, which BytesIO will not say."""

    def __init__(self, payload=AUDIO):
        super().__init__(payload)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


async def _drive(response, *, disconnect_after=None):
    """Run a response as an ASGI server would.

    @param disconnect_after Number of body chunks to accept before the
        client is reported gone, or None to let it finish.
    """
    sent = []

    async def receive():
        if disconnect_after is None:
            await asyncio.sleep(3600)
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] != "http.response.body":
            return
        sent.append(message.get("body", b""))
        if disconnect_after is not None:
            await asyncio.sleep(0.01)

    await response({"type": "http", "method": "GET"}, receive, send)
    return b"".join(sent)


class TestWhoClosesTheStream:
    @pytest.mark.asyncio
    async def test_a_response_that_runs_to_the_end(self):
        handle = _Handle()
        body = await _drive(
            stream_response(
                handle, span=None, size=len(AUDIO), mime_type="audio/flac"
            )
        )
        assert body == AUDIO
        assert handle.was_closed

    @pytest.mark.asyncio
    async def test_a_client_that_goes_away_mid_track(self):
        """A ``finally`` inside the body generator does not run here: the
        consumer is cancelled and the generator is left suspended at its
        ``yield`` until the loop finalizes async generators, which happens at
        shutdown. A seeking renderer opens a ranged request per seek, so the
        handles would pile up on the share.
        """
        handle = _Handle(AUDIO * 4096)  # large enough to still be streaming
        await _drive(
            stream_response(
                handle,
                span=None,
                size=len(AUDIO) * 4096,
                mime_type="audio/flac",
            ),
            disconnect_after=1,
        )
        assert handle.was_closed


class TestOpeningTheStream:
    @pytest.mark.asyncio
    async def test_a_stream_that_arrives_after_the_timeout(self):
        handle = _Handle()

        def slow():
            import time

            time.sleep(0.3)
            return handle

        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await open_reader(slow, 0.05)

        await asyncio.sleep(0.5)
        assert handle.was_closed

    @pytest.mark.asyncio
    async def test_a_request_cancelled_while_the_storage_is_opening(self):
        """``CancelledError`` is a ``BaseException``, so the cleanup cannot
        be hung off ``except TimeoutError``. The open is shielded and runs to
        completion regardless, handing its stream to a waiter that no longer
        exists."""
        handle = _Handle()

        def slow():
            import time

            time.sleep(0.3)
            return handle

        task = asyncio.ensure_future(open_reader(slow, 10.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.5)
        assert handle.was_closed

    @pytest.mark.asyncio
    async def test_an_open_that_fails_reports_its_own_error(self):
        def refuse():
            raise OSError("logon failure")

        with pytest.raises(OSError, match="logon failure"):
            await open_reader(refuse, 5.0)


class TestWhatARangeMeans:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("bytes=0-99", (0, 99)),
            ("bytes=100-", (100, 1999)),
            ("bytes=-500", (1500, 1999)),
            ("bytes=0-99999", (0, 1999)),
            ("bytes=1999-1999", (1999, 1999)),
        ],
    )
    def test_a_span_that_is_honoured(self, header, expected):
        assert resolve_range(header, 2000) == expected

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "furlongs=0-1",
            "bytes=0-0,5-6",
            "bytes=abc-def",
            "bytes=--5",
            "bytes=0",
        ],
    )
    def test_a_header_not_worth_honouring_serves_the_whole_asset(self, header):
        """The specification calls for ignoring a range this server cannot
        satisfy the form of, rather than refusing the request — and no
        renderer asks for several spans at once."""
        assert resolve_range(header, 2000) is None

    @pytest.mark.parametrize("header", ["bytes=2000-", "bytes=99-5", "bytes=-0"])
    def test_a_span_outside_the_asset_is_refused(self, header):
        with pytest.raises(UnsatisfiableRange):
            resolve_range(header, 2000)

    @pytest.mark.parametrize("header", ["bytes=0-", "bytes=-500"])
    def test_a_zero_length_asset_satisfies_no_range(self, header):
        """A track truncated to nothing — an interrupted copy, a re-rip in
        progress — still passes the existence check. Answering the suffix
        form with a span produced ``Content-Range: bytes 0--1/0``, which is
        where a renderer reads the stream size from.
        """
        with pytest.raises(UnsatisfiableRange):
            resolve_range(header, 0)


class TestHeadMatchesGet:
    @pytest.mark.asyncio
    async def test_the_headers_are_the_same_ones(self):
        span = resolve_range("bytes=10-19", len(AUDIO))
        head = head_response(span, len(AUDIO), "audio/flac")
        get = stream_response(
            _Handle(), span=span, size=len(AUDIO), mime_type="audio/flac"
        )

        assert head.status_code == get.status_code == 206
        for field in ("content-range", "content-length", "accept-ranges"):
            assert head.headers[field] == get.headers[field]
