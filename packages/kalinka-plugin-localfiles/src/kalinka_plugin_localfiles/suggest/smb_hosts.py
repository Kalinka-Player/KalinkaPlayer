"""File servers on the network, offered as the server of a music source.

What is suggested is a host, not a share: the share on it still has to be
named, and only the server knows what it is called.
"""

from __future__ import annotations

from kalinka_plugin_sdk import ConfigOption

from .smb_discovery import DiscoveredHost, SmbHostDiscovery


def host_option(host: DiscoveredHost) -> ConfigOption:
    """One server as a suggestion.

    The address is what gets stored, even where the host announced a name:
    a name learned from mDNS or NetBIOS is not one this machine's resolver
    can necessarily look up, and a server that cannot be reached is worse
    than one that is harder to read.
    """
    found = f"found over {host.source}"
    return ConfigOption(
        value=host.address,
        label=host.name or host.address,
        description=f"{host.address} · {found}" if host.name else found,
    )


class SmbHostSuggester:
    """Music sources on servers this machine can see.

    @note Does not own the discovery it reads — one listener serves every
        question asked of it, and starting it is the plugin's to do.
    """

    def __init__(self, discovery: SmbHostDiscovery) -> None:
        self._discovery = discovery

    def options(self) -> list[ConfigOption]:
        return [host_option(host) for host in self._discovery.hosts()]

    def refresh(self) -> None:
        self._discovery.refresh()
