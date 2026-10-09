import socket
import fcntl
import struct
import ifaddr


def get_interface_ip_mappings() -> dict[str, list[str]]:
    """
    Get a mapping of network interface names to their IP addresses.
    Excludes loopback interfaces.
    Returns:
        Dictionary mapping interface names to all their IPv4 addresses.
    """
    interface_ips: dict[str, list[str]] = {}

    for adapter in ifaddr.get_adapters():
        if adapter.name == "lo":
            continue

        addresses: list[str] = []
        for ip in adapter.ips:
            # ifaddr gives an IPv4 address as a string, an IPv6 one as a tuple.
            if (
                isinstance(ip.ip, str)
                and not ip.ip.startswith("127.")
                and ip.ip not in addresses
            ):
                addresses.append(ip.ip)
        if addresses:
            interface_ips[adapter.name] = addresses

    return interface_ips


def server_base_url(server_addr: tuple[str, int]) -> str:
    """``http://host:port`` for one of this server's own addresses, with an
    IPv6 host bracketed."""
    host, port = server_addr
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def get_ip_address(interface: str) -> str:
    """
    Uses the Linux SIOCGIFADDR ioctl to find the IP address associated
    with a network interface, given the name of that interface, e.g.
    "eth0". Only works on GNU/Linux distributions.
    Source: https://bit.ly/3dROGBN
    Returns:
        The IP address in quad-dotted notation of four decimal integers.
    """

    if interface == "all":
        return "0.0.0.0"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    packed_iface = struct.pack("256s", interface.encode("utf_8"))
    packed_addr = fcntl.ioctl(sock.fileno(), 0x8915, packed_iface)[20:24]
    return socket.inet_ntoa(packed_addr)
