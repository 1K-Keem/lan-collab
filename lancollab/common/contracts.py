"""Shared interface CONTRACTS for parallel implementation.

These are typed data structures and ports, not network operations.
TV1/TV2/TV3 must review docs/protocol-spec.md before changing fields.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

PROTOCOL_VERSION = 1
MAX_JSON_BYTES = 262_144
FILE_CHUNK_BYTES = 16 * 1024


@dataclass(frozen=True)
class Envelope:
    """Proposed v1 message envelope. Production code must validate input."""

    type: str
    request_id: str
    payload: dict[str, Any]
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class SessionIdentity:
    """Server-side session context; username comes from authentication."""

    remote_ip: str
    remote_port: int
    username: str | None = None


@dataclass(frozen=True)
class OutboundEvent:
    """Destination username + event to enqueue; not proof of delivery."""

    recipient: str
    message: Envelope


@runtime_checkable
class MessageTransport(Protocol):
    """One message per call; implementations own socket framing and TLS."""

    def send(self, message: Envelope) -> None: ...

    def receive(self, *, timeout: float | None = None) -> Envelope: ...

    def close(self) -> None: ...


class ProtocolCodec(Protocol):
    """Full TCP-frame encoder/decoder. TV2 implements production codec."""

    def encode(self, message: Envelope) -> bytes: ...

    def decode(self, frame: bytes) -> Envelope: ...
