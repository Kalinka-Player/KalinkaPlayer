"""Discovery runs SSDP once per IPv4 address this machine has outside
loopback."""

from __future__ import annotations

import ifaddr
import pytest

from kalinka_plugin_musiccast import musiccast


def fake_adapter(name, *addresses):
    return ifaddr.Adapter(
        name,
        name,
        [
            ifaddr.IP(address, 64 if isinstance(address, tuple) else 24, name)
            for address in addresses
        ],
    )


@pytest.mark.unit
def test_every_routable_ipv4_address_is_a_discovery_interface(monkeypatch):
    monkeypatch.setattr(
        musiccast.ifaddr,
        "get_adapters",
        lambda: [
            fake_adapter("lo", "127.0.0.1", ("::1", 0, 0)),
            fake_adapter("eth0", "192.168.1.2", ("fe80::1", 0, 2), "10.0.0.7"),
            fake_adapter("wlan0", ("fe80::2", 0, 3)),
            fake_adapter("dummy0", "0.0.0.0"),
        ],
    )

    assert musiccast.get_network_interfaces() == [
        ("eth0", "192.168.1.2"),
        ("eth0", "10.0.0.7"),
    ]
