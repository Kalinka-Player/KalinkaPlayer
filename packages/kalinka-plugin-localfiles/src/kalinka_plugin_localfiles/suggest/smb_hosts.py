"""File servers on the network, offered as the start of a music folder.

What is suggested is a host, not a folder: the share on it still has to be
named, and only the server knows what it is called. The value therefore
ends at the separator the share goes after, and saving it as it stands is
refused with the sentence that says so.
"""

from __future__ import annotations

from kalinka_plugin_sdk import ConfigOption

from ..storage.locator import SMB_SCHEME
from .smb_discovery import DiscoveredHost, SmbHostDiscovery


def _authority(address: str) -> str:
    """An address as a URL spells it. An IPv6 literal goes in brackets, so
    the colons in it are not read as a port."""
    return f"[{address}]" if ":" in address else address


def host_option(host: DiscoveredHost) -> ConfigOption:
    """One server as a suggestion.

    The address is what gets stored, even where the host announced a name:
    a name learned from mDNS or NetBIOS is not one this machine's resolver
    can necessarily look up, and a folder that cannot be opened is worse
    than one that is harder to read.
    """
    found = f"found over {host.source}"
    return ConfigOption(
        value=f"{SMB_SCHEME}://{_authority(host.address)}/",
        label=host.name or host.address,
        description=f"{host.address} · {found}" if host.name else found,
    )


class SmbHostSuggester:
    """Music folders on servers this machine can see.

    @note Does not own the discovery it reads — one listener serves every
        question asked of it, and starting it is the plugin's to do.
    """

    def __init__(self, discovery: SmbHostDiscovery) -> None:
        self._discovery = discovery

    def options(self) -> list[ConfigOption]:
        return [host_option(host) for host in self._discovery.hosts()]

    def refresh(self) -> None:
        self._discovery.refresh()
