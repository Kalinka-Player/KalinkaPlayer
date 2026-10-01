import asyncio
from unittest.mock import Mock

import pytest
from kalinka_plugin_upnp.discovery import Discovery
from kalinka_plugin_upnp.services import DEVICE_TYPE


@pytest.fixture
async def discovery():
    discovery = Discovery(
        "192.0.2.1", "http://192.0.2.1:49152/description.xml", "uuid:test-device"
    )
    discovery.connection_made(Mock())
    yield discovery
    await discovery.close()


def search(target, mx="1"):
    return f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: {mx}\r\nST: {target}\r\n\r\n'.encode()


async def replies(discovery, packet, monkeypatch):
    monkeypatch.setattr("kalinka_plugin_upnp.discovery.random.uniform", lambda a, b: 0)
    discovery.datagram_received(packet, ("192.0.2.2", 54321))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return discovery.transport.sendto.call_args_list


@pytest.mark.parametrize(
    "target,count",
    [
        ("ssdp:all", 6),
        ("upnp:rootdevice", 1),
        (DEVICE_TYPE, 1),
        ("uuid:test-device", 1),
        ("urn:unknown", 0),
    ],
)
async def test_search_targets(discovery, monkeypatch, target, count):
    calls = await replies(discovery, search(target), monkeypatch)
    assert len(calls) == count
    for call in calls:
        data, address = call.args
        assert address == ("192.0.2.2", 54321)
        assert data.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"LOCATION: http://192.0.2.1:49152/description.xml\r\n" in data
        assert b"USN: uuid:test-device" in data
        assert data.endswith(b"\r\n\r\n")


@pytest.mark.parametrize(
    "packet",
    [
        b"garbage",
        b"\xff",
        search("ssdp:all", "bad"),
        search("ssdp:all", "0"),
        search("ssdp:all").replace(b'"ssdp:discover"', b'"other"'),
    ],
)
async def test_malformed_search_is_ignored(discovery, monkeypatch, packet):
    assert await replies(discovery, packet, monkeypatch) == []


async def test_advertisement_and_shutdown_cancel_pending_replies(discovery):
    transport = discovery.transport
    discovery.advertise("ssdp:alive")
    assert transport.sendto.call_count == 6
    for call in transport.sendto.call_args_list:
        assert b"NTS: ssdp:alive" in call.args[0]
    discovery.datagram_received(search("ssdp:all"), ("192.0.2.2", 54321))
    handles = list(discovery.pending)
    await discovery.close()
    assert transport.sendto.call_count == 12
    assert all(handle.cancelled() for handle in handles)
    assert not discovery.pending
    transport.close.assert_called_once()


async def test_search_flood_has_bounded_pending_timers(discovery):
    for _ in range(100):
        discovery.datagram_received(search("ssdp:all"), ("192.0.2.2", 54321))
    assert len(discovery.pending) == 64
