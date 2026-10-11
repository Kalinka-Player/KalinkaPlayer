import asyncio
import os

import pytest

from kalinka_plugin_sdk.live_capture import LiveCapture, LiveCaptureError
from kalinka_plugin_sdk.live_content import LiveContent, LiveContentError

MIB = 1024 * 1024


async def _refusal(capture, start, end=None) -> int:
    with pytest.raises(LiveContentError) as error:
        await capture.open(start, end)
    return error.value.status


async def _read(reader, count: int) -> bytes:
    data = bytearray()
    while len(data) < count and (chunk := await reader.read(count - len(data))):
        data += chunk
    return bytes(data)


def test_a_capture_is_live_content(tmp_path):
    assert isinstance(LiveCapture(tmp_path), LiveContent)


async def test_a_pause_in_production_is_not_the_end(tmp_path):
    c = LiveCapture(tmp_path)
    r = await c.open(0, None)
    pending = asyncio.create_task(r.read(10))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await c.append(b"abc")
    assert await pending == b"abc"
    assert c.size is None

    pending = asyncio.create_task(r.read(10))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await c.finish()
    assert await pending == b""
    assert c.size == 3 and c.delivered == 3
    await r.aclose()
    await c.retire()


async def test_bytes_stay_until_the_last_reader_closes(tmp_path):
    c = LiveCapture(tmp_path)
    r = await c.open(0, None)
    await c.retire(wait_for_disconnect=True)
    assert not c.closed
    await r.aclose()
    assert c.closed
    await asyncio.wait_for(c.wait_closed(), 1)


async def test_a_reader_left_open_drains_what_was_captured(tmp_path):
    c = LiveCapture(tmp_path)
    r = await c.open(0, None)
    await c.append(b"tail")
    await c.finish()
    await c.retire(wait_for_disconnect=True)
    assert await r.read(100) == b"tail"
    assert await r.read(100) == b""
    await r.aclose()
    assert c.closed


async def test_an_unfinished_capture_left_open_waits_for_the_hang_up(tmp_path):
    c = LiveCapture(tmp_path)
    r = await c.open(0, None)
    await c.append(b"ab")
    await c.retire(wait_for_disconnect=True)
    assert await r.read(100) == b"ab"
    pending = asyncio.create_task(r.read(100))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await r.aclose()
    with pytest.raises(OSError):
        await pending


async def test_retiring_fails_a_waiting_reader(tmp_path):
    c = LiveCapture(tmp_path)
    r = await c.open(0, None)
    pending = asyncio.create_task(r.read(100))
    await asyncio.sleep(0)
    await c.retire()
    with pytest.raises(OSError, match="cancelled"):
        await pending
    await r.aclose()
    assert await _refusal(c, 0) == 410


async def test_a_retired_capture_takes_no_bytes(tmp_path):
    c = LiveCapture(tmp_path)
    await c.retire()
    with pytest.raises(LiveCaptureError):
        await c.append(b"stale")


async def test_an_unfinished_capture_plays_once_from_its_start(tmp_path):
    c = LiveCapture(tmp_path)
    await c.append(b"abc")
    assert await _refusal(c, 1) == 409
    r = await c.open(0, None)
    await r.aclose()
    assert await _refusal(c, 0) == 409

    await c.finish()
    r = await c.open(1, 3)
    assert await r.read(100) == b"bc"
    assert await r.read(100) == b""
    await r.aclose()
    await c.retire()


async def test_bytes_not_yet_captured_are_refused(tmp_path):
    c = LiveCapture(tmp_path)
    await c.append(b"abc")
    await c.finish()
    assert await _refusal(c, 4) == 409
    await c.retire()


async def test_readers_are_limited(tmp_path):
    c = LiveCapture(tmp_path, max_readers=1)
    await c.finish()
    r = await c.open(0, None)
    assert await _refusal(c, 0) == 429
    await r.aclose()
    await c.retire()


async def test_the_byte_limit_fails_the_capture(tmp_path):
    c = LiveCapture(tmp_path, max_bytes=4)
    r = await c.open(0, None)
    await c.append(b"abcd")
    with pytest.raises(LiveCaptureError, match="outgrew"):
        await c.append(b"e")
    assert c.available == 4
    with pytest.raises(OSError, match="outgrew"):
        await r.read(1)
    await r.aclose()
    await c.retire()
    assert c.closed


async def test_bytes_span_the_capture_files(tmp_path):
    payload = os.urandom(2 * MIB + 123)
    c = LiveCapture(tmp_path)
    await c.append(payload)
    await c.finish()
    r = await c.open(0, None)
    assert await _read(r, len(payload) + 1) == payload
    await r.aclose()
    r = await c.open(MIB - 1, MIB + 1)
    assert await _read(r, 3) == payload[MIB - 1 : MIB + 1]
    await r.aclose()
    await c.retire()


async def test_a_capture_keeping_nothing_delivered_bounds_only_what_is_unread(tmp_path):
    c = LiveCapture(tmp_path, max_bytes=2 * MIB, keep_delivered=False)
    r = await c.open(0, None)
    for _ in range(5):
        payload = os.urandom(MIB)
        await c.append(payload)
        assert await _read(r, MIB) == payload
    await c.finish()
    assert await r.read(1) == b""
    await r.aclose()
    assert c.available == 5 * MIB
    assert await _refusal(c, 0) == 409
    await c.retire()


async def test_a_reader_falling_behind_fails_a_capture_keeping_nothing(tmp_path):
    c = LiveCapture(tmp_path, max_bytes=4, keep_delivered=False)
    r = await c.open(0, None)
    await c.append(b"abcd")
    assert await r.read(2) == b"ab"
    await c.append(b"ef")
    with pytest.raises(LiveCaptureError, match="outgrew"):
        await c.append(b"g")
    await r.aclose()
    await c.retire()


async def test_a_failed_write_fails_the_capture(tmp_path, monkeypatch):
    c = LiveCapture(tmp_path)

    def disk_full(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "pwrite", disk_full)
    with pytest.raises(LiveCaptureError, match="could not be written"):
        await c.append(b"abc")
    assert c.available == 0
    assert await _refusal(c, 0) == 410
    await c.retire()


async def test_a_read_waits_only_so_long(tmp_path):
    c = LiveCapture(tmp_path, read_timeout_s=0.01)
    r = await c.open(0, None)
    with pytest.raises(OSError, match="in time"):
        await r.read(1)
    pending = asyncio.create_task(r.read(1))
    await asyncio.sleep(0)
    await r.aclose()
    with pytest.raises(OSError, match="cancelled"):
        await pending
    await c.retire()


async def test_a_write_outlives_a_repeated_cancel(tmp_path, monkeypatch):
    c = LiveCapture(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    write = os.pwrite

    def slow_write(fd, data, offset):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        return write(fd, data, offset)

    monkeypatch.setattr(os, "pwrite", slow_write)
    pending = asyncio.create_task(c.append(b"abc"))
    await started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await c.retire()
    assert c.closed
