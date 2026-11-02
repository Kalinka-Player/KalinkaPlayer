import asyncio
import struct

import pytest

from kalinka_plugin_sdk.live_audio import LivePolicy, PcmFormat
from kalinka_plugin_sdk.live_content import LiveContentError
from kalinka_server.live_audio import HEADER, MAGIC, RECORD, LiveAudioRegistry


@pytest.fixture
def registry():
    service = LiveAudioRegistry()
    yield service
    service.close()


@pytest.mark.asyncio
async def test_preparation_sends_only_header_then_starts_with_fresh_frames(registry):
    source = registry.for_plugin("airplay").create_pcm(PcmFormat(48000, 2, "S24_3LE"))
    source.offer(b"\x01" * 480 * 6)
    route = registry.bind(source.reference, LivePolicy())
    reader = await route.open(0, None)
    header = await reader.read(1000)
    assert HEADER.unpack(header)[:5] == (MAGIC, 48000, 2, 2, 1)
    pending = asyncio.create_task(reader.read(10000))
    await asyncio.sleep(0)
    source.offer(b"\x02" * 480 * 6)
    await asyncio.sleep(0)
    assert not pending.done()
    route.activate()
    source.offer(b"\x03" * 480 * 6)
    data = await asyncio.wait_for(pending, 1)
    assert RECORD.unpack(data[:16]) == (960, 480, 2880)
    assert data[16:] == b"\x03" * 2880
    await reader.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "layout,frame",
    [
        ("S16_LE", struct.pack("<hh", -32768, 32767)),
        ("S24_3LE", bytes.fromhex("010080ffff7f")),
        ("S24_LE", struct.pack("<ii", -8388608, 8388607)),
        ("S32_LE", struct.pack("<ii", -2147483648, 2147483647)),
        ("FLOAT_LE", struct.pack("<ff", -0.5, 0.25)),
    ],
)
async def test_partial_http_reads_preserve_layout_and_samples(registry, layout, frame):
    source = registry.for_plugin("bt").create_pcm(PcmFormat(44100, 2, layout))
    route = registry.bind(source.reference, LivePolicy())
    reader = await route.open(0, None)
    route.activate()
    pcm = frame * 441
    source.offer(pcm)
    expected_size = HEADER.size + RECORD.size + len(pcm)
    data = bytearray()
    while len(data) < expected_size:
        data.extend(await asyncio.wait_for(reader.read(7), 1))
    assert data[HEADER.size + RECORD.size :] == pcm
    assert route.first_frame == 0
    with pytest.raises(LiveContentError):
        await route.open(0, None)
    await reader.aclose()


@pytest.mark.asyncio
async def test_slow_reader_is_ended_without_backpressuring_capture(registry):
    source = registry.for_plugin("bt").create_pcm(PcmFormat(48000), max_buffer_ms=100)
    route = registry.bind(source.reference, LivePolicy())
    reader = await route.open(0, None)
    await reader.read(32)
    route.activate()
    for _ in range(100):
        source.offer(bytes(480 * 4))
    assert sum(len(block.pcm) for block in source.blocks) <= 4800 * 4
    assert source.frames == 48000
    assert await reader.read(1024) == b""
    assert route.closed and not source.closed
    fresh = registry.bind(source.reference, LivePolicy())
    fresh.activate()
    assert fresh.cursor == source.frames


@pytest.mark.asyncio
async def test_reset_and_close_wake_waiting_readers(registry):
    source = registry.for_plugin("bt").create_pcm(PcmFormat(48000))
    for reset in (True, False):
        route = registry.bind(source.reference, LivePolicy())
        reader = await route.open(0, None)
        await reader.read(32)
        pending = asyncio.create_task(reader.read(10))
        await asyncio.sleep(0)
        if reset:
            source.reset(PcmFormat(44100, 2, "S24_3LE"))
            assert source.epoch == 2
        else:
            source.close()
        assert await asyncio.wait_for(pending, 1) == b""
        assert route.closed
    assert not registry.sources


def test_feedback_bounds_audio_already_delivered_to_network(registry):
    source = registry.for_plugin("bt").create_pcm(PcmFormat(48000))
    route = registry.bind(source.reference, LivePolicy())
    route.first_frame = 1000
    source.frames = 1000 + 48000
    assert route.feedback(900)
    assert not route.feedback(500)
    assert not source.closed


def test_ownership_partial_frames_and_memory_limits(registry):
    source = registry.for_plugin("airplay").create_pcm(PcmFormat(48000, 2, "S24_3LE"))
    with pytest.raises(ValueError, match="another plugin"):
        registry.resolve(source.reference, "bt")
    with pytest.raises(ValueError, match="complete frames"):
        source.offer(b"12345")
    with pytest.raises(ValueError):
        LivePolicy(target_latency_ms=250, max_buffer_ms=100)
    with pytest.raises(ValueError):
        PcmFormat(48000, 8)
    source.close()
    with pytest.raises(RuntimeError):
        source.offer(b"123456")


@pytest.mark.asyncio
async def test_http_route_does_not_cache_seek_or_consume_a_reader_on_head(registry):
    import httpx
    from fastapi import FastAPI
    from kalinka_server.live_audio import register_live_audio_route

    app = FastAPI()
    register_live_audio_route(app, registry)
    source = registry.for_plugin("bt").create_pcm(PcmFormat(48000))
    route = registry.bind(source.reference, LivePolicy())
    path = f"/live-audio/{route.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://core"
    ) as client:
        head = await client.head(path)
        assert head.status_code == 200 and not route.opened
        assert "content-length" not in head.headers
        assert head.headers["accept-ranges"] == "none"
        assert head.headers["content-encoding"] == "identity"
        assert head.headers["cache-control"] == "no-store, no-transform"
        assert head.headers["x-accel-buffering"] == "no"
        assert (
            await client.get(path, headers={"range": "bytes=16-"})
        ).status_code == 409
        route.close()
        assert (await client.get(path)).status_code == 410
        assert (await client.get("/live-audio/unknown")).status_code == 410
