"""What to offer the user when they are asked where their music is.

Answering that is not the same job as reading the files once they have
said: the storages are stateless and built in every worker process, while a
suggestion comes from watching the network over time and is wanted in one
process only. This package is that process's side of it.
"""

from .base import RootSuggester
from .local_mounts import LocalMountSuggester, mount_options
from .smb_discovery import DiscoveredHost, SmbHostDiscovery
from .smb_hosts import SmbHostSuggester, host_option

__all__ = [
    "DiscoveredHost",
    "LocalMountSuggester",
    "RootSuggester",
    "SmbHostDiscovery",
    "SmbHostSuggester",
    "host_option",
    "mount_options",
]
