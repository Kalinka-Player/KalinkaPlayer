"""The IPv4 addresses each network interface of this machine has."""

import ipaddress

from kalinka_server import netutils
from tests.ifaddr_fake import fake_adapters


def test_interface_mapping_keeps_every_distinct_ipv4_address(monkeypatch):
    fake_adapters(
        monkeypatch,
        {
            "lo": ["127.0.0.1", ("::1", 0, 0)],
            "eth0": [
                "192.168.1.20",
                ("fe80::1", 0, 2),
                "10.20.0.15",
                "192.168.1.20",
                "127.0.0.2",
            ],
            "wlan0": [("fe80::2", 0, 3)],
        },
    )

    assert netutils.get_interface_ip_mappings() == {
        "eth0": ["192.168.1.20", "10.20.0.15"]
    }


def test_interface_mapping_reads_this_machines_adapters():
    mapping = netutils.get_interface_ip_mappings()

    assert "lo" not in mapping
    for addresses in mapping.values():
        assert addresses
        for address in addresses:
            assert ipaddress.ip_address(address).version == 4
            assert not address.startswith("127.")
