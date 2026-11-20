"""Best-effort local IPv4 discovery using only the standard library.

Hosts with VMware, VirtualBox, Hyper-V or VPN adapters have several IPv4
addresses; only the one on the default route (usually Wi-Fi/Ethernet) is
reachable by other machines on the LAN, so it is reported first.
"""

import ipaddress
import socket


def primary_ipv4() -> str | None:
    """Address of the adapter holding the default route; nothing is sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address in ("0.0.0.0", "") else address


def local_ipv4_addresses() -> list[str]:
    """Primary address first, then the other adapters' addresses (no loopback)."""
    found: list[str] = []
    primary = primary_ipv4()
    if primary is not None:
        found.append(primary)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        found += [info[4][0] for info in infos]
    except OSError:
        pass
    return [a for a in dict.fromkeys(found) if not ipaddress.IPv4Address(a).is_loopback]
