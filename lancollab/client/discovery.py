"""UDP discovery and network-quality probes (not an authentication channel).

Datagrams are bare JSON, <= 1200 bytes. Each client binds an ephemeral port.
The server responder and verified TCP/TLS connection live in separate modules.
"""

from dataclasses import dataclass
import ipaddress
import json
import socket
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

from lancollab.common.netinfo import local_ipv4_addresses
from lancollab.common.udp_validation import (
    UDP_MAX_BYTES,
    UDP_PORT,
    reject_duplicate_keys,
    validate_datagram_size,
    validate_udp_message,
)


@dataclass(frozen=True)
class ServerEndpoint:
    host: str
    tcp_port: int
    server_name: str
    discovered: bool


@dataclass(frozen=True)
class NetworkQuality:
    sent: int
    received: int
    loss_percent: float
    last_rtt_ms: float | None


LIMITED_BROADCAST = "255.255.255.255"
# Netmasks are not portable to read from the stdlib, so guess the common LAN sizes.
_GUESSED_PREFIXES = range(16, 25)


def broadcast_targets(local_addresses: list[str]) -> list[str]:
    """Limited broadcast first, then subnet-directed broadcasts for private adapters.

    Windows with several adapters (VPN, VMware, VirtualBox) may send
    255.255.255.255 out of only one of them, and some Wi-Fi networks drop it;
    a directed broadcast such as 10.127.255.255 is routed out the right adapter.
    """
    targets = [LIMITED_BROADCAST]
    for address in local_addresses:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if not ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        for prefix in _GUESSED_PREFIXES:
            network = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
            target = str(network.broadcast_address)
            if target != address and target not in targets:
                targets.append(target)
    return targets


class DiscoveryClient:
    """One ephemeral UDP socket per client; not shared with authenticated TCP.

    The optional socket/clock factories support deterministic tests; a caller
    may use a single object for both discovery and subsequent heartbeat probes.
    """

    def __init__(
        self,
        *,
        udp_port: int = UDP_PORT,
        socket_factory: Callable[..., Any] = socket.socket,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if type(udp_port) is not int or not 1 <= udp_port <= 65535:
            raise ValueError("Invalid UDP port")
        self.instance_id = uuid4().hex
        self.udp_port = udp_port
        self._clock = clock
        self._socket = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socket.bind(("", 0))
        self._seq = 0
        self._sent = 0
        self._received = 0
        self._last_rtt_ms: float | None = None
        self._closed = False

    def _wait_for(
        self,
        *,
        timeout: float,
        predicate: Callable[[dict[str, Any], str], bool],
    ) -> tuple[dict[str, Any], str] | None:
        deadline = self._clock() + timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            self._socket.settimeout(remaining)
            try:
                raw, source = self._socket.recvfrom(UDP_MAX_BYTES + 1)
            except (socket.timeout, TimeoutError):
                return None
            try:
                validate_datagram_size(raw)
                message = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
                validate_udp_message(message)
            except (ValueError, UnicodeDecodeError):
                continue
            if predicate(message, source[0]):
                return message, source[0]

    def _send(self, message: dict[str, Any], host: str) -> None:
        validate_udp_message(message)
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        validate_datagram_size(data)
        self._socket.sendto(data, (host, self.udp_port))

    def discover(
        self,
        *,
        timeout: float = 4.0,
        broadcast_host: str | None = None,
        attempts: int = 4,
    ) -> ServerEndpoint | None:
        """Send DISCOVER to ``broadcast_host``, or to every likely LAN broadcast if omitted.

        Shared Wi-Fi often delays (~1.5 s) or drops broadcasts, so the request is
        resent ``attempts`` times across ``timeout`` and the first reply wins.
        """
        if timeout <= 0 or attempts < 1:
            raise ValueError("timeout and attempts must be positive")
        hosts = (
            [broadcast_host]
            if broadcast_host is not None
            else broadcast_targets(local_ipv4_addresses())
        )
        request = {"version": 1, "type": "DISCOVER", "instance_id": self.instance_id}

        def valid(message: dict[str, Any], source: str) -> bool:
            return (
                message["type"] == "DISCOVER_REPLY"
                and message["instance_id"] == self.instance_id
                and bool(source)
            )

        for _attempt in range(attempts):
            error: OSError | None = None
            delivered = False
            for host in hosts:
                try:
                    self._send(request, host)
                    delivered = True
                except OSError as exc:
                    error = exc
            if not delivered and error is not None:
                raise error
            received = self._wait_for(timeout=timeout / attempts, predicate=valid)
            if received is not None:
                message, host = received
                return ServerEndpoint(host, message["tcp_port"], message["server_name"], True)
        return None

    def heartbeat(self, host: str, *, timeout: float = 1.0) -> NetworkQuality:
        if not host or timeout <= 0:
            raise ValueError("Host and positive timeout are required")
        self._seq += 1
        seq = self._seq
        self._sent += 1
        start = self._clock()
        self._send(
            {"version": 1, "type": "HEARTBEAT", "instance_id": self.instance_id, "seq": seq},
            host,
        )

        def valid(message: dict[str, Any], source: str) -> bool:
            return (
                source == host
                and message["type"] == "HEARTBEAT_ACK"
                and message["instance_id"] == self.instance_id
                and message["seq"] == seq
            )

        if self._wait_for(timeout=timeout, predicate=valid) is not None:
            self._received += 1
            self._last_rtt_ms = (self._clock() - start) * 1000
        return self.quality

    @property
    def quality(self) -> NetworkQuality:
        sent = self._sent
        return NetworkQuality(
            sent=sent,
            received=self._received,
            loss_percent=(100 * (sent - self._received) / sent) if sent else 0.0,
            last_rtt_ms=self._last_rtt_ms,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._socket.close()

    def __enter__(self) -> "DiscoveryClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def select_server(
    discovery: ServerEndpoint | None, *, host: str | None, port: int = 40000
) -> ServerEndpoint:
    """Prefer explicit --host (TLS identity still verified independently)."""
    if host:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid TCP port")
        return ServerEndpoint(host, port, "manual", False)
    if discovery is None:
        raise ConnectionError("No UDP discovery reply; specify --host and --port")
    return discovery
