"""Network interfaces for netutils to find, in place of the machine's own."""

import ifaddr

from kalinka_server import netutils


def fake_adapters(monkeypatch, adapters: dict[str, list]) -> None:
    """Have ifaddr report ``adapters``: interface name to addresses, an IPv4
    address as a string and an IPv6 one as an (address, flowinfo, scope_id)
    tuple, as ifaddr gives them."""
    monkeypatch.setattr(
        netutils.ifaddr,
        "get_adapters",
        lambda: [
            ifaddr.Adapter(
                name,
                name,
                [
                    ifaddr.IP(address, 64 if isinstance(address, tuple) else 24, name)
                    for address in addresses
                ],
            )
            for name, addresses in adapters.items()
        ],
    )
