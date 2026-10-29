"""TV3 UDP discovery and network-quality probes (not an authentication channel).

Datagrams are bare JSON, <= 1200 bytes. Each client binds an ephemeral port.
The server implementation and verified TCP/TLS connection belong to TV1/TV2.
"""

from dataclasses import dataclass
import json
import socket
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

UDP_MAX_BYTES = 1200
UDP_PORT = 40001


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


def _decode(raw: bytes) -> dict[str, Any] | None:
    if not 0 < len(raw) <= UDP_MAX_BYTES:
        return None

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("duplicate UDP field")
            output[key] = value
        return output

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _encode(message: dict[str, Any]) -> bytes:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > UDP_MAX_BYTES:
        raise ValueError("UDP datagram exceeds 1200 bytes")
    return data


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
            message = _decode(raw)
            if message is not None and predicate(message, source[0]):
                return message, source[0]

    def discover(
        self, *, timeout: float = 1.5, broadcast_host: str = "255.255.255.255"
    ) -> ServerEndpoint | None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._socket.sendto(
            _encode({"version": 1, "type": "DISCOVER", "instance_id": self.instance_id}),
            (broadcast_host, self.udp_port),
        )

        def valid(message: dict[str, Any], source: str) -> bool:
            return (
                set(message) == {"version", "type", "instance_id", "server_name", "tcp_port"}
                and type(message["version"]) is int
                and message["version"] == 1
                and message["type"] == "DISCOVER_REPLY"
                and message["instance_id"] == self.instance_id
                and isinstance(message["server_name"], str)
                and bool(message["server_name"])
                and type(message["tcp_port"]) is int
                and 1 <= message["tcp_port"] <= 65535
                and bool(source)
            )

        received = self._wait_for(timeout=timeout, predicate=valid)
        if received is None:
            return None
        message, host = received
        return ServerEndpoint(host, message["tcp_port"], message["server_name"], True)

    def heartbeat(self, host: str, *, timeout: float = 1.0) -> NetworkQuality:
        if not host or timeout <= 0:
            raise ValueError("Host and positive timeout are required")
        self._seq += 1
        seq = self._seq
        self._sent += 1
        start = self._clock()
        self._socket.sendto(
            _encode(
                {"version": 1, "type": "HEARTBEAT", "instance_id": self.instance_id, "seq": seq}
            ),
            (host, self.udp_port),
        )

        def valid(message: dict[str, Any], source: str) -> bool:
            return (
                source == host
                and set(message) == {"version", "type", "instance_id", "seq"}
                and type(message["version"]) is int
                and message["version"] == 1
                and message["type"] == "HEARTBEAT_ACK"
                and message["instance_id"] == self.instance_id
                and type(message["seq"]) is int
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