"""Stable typed interfaces for 3-way parallel implementation.

This module declares boundaries only. No TCP, TLS, UDP or real protocol
validation is implemented here; see docs/protocol-spec.md revision 1.1.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

PROTOCOL_VERSION = 1  # Wire version remains 1; documentation revision is 1.1.
MAX_JSON_BYTES = 262_144
FILE_CHUNK_BYTES = 16 * 1024
FILE_WINDOW_CHUNKS = 8
FILE_ACK_INTERVAL_CHUNKS = 4


@dataclass(frozen=True)
class Envelope:
    """Versioned message envelope; real codec must validate its payload."""

    type: str
    request_id: str
    payload: dict[str, Any]
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class SessionIdentity:
    """Server session identity, derived from authentication, not client payload."""

    remote_ip: str
    remote_port: int
    username: str | None = None


@dataclass(frozen=True)
class OutboundEvent:
    """Event queued for a recipient; not proof of delivery/read."""

    recipient: str
    message: Envelope


@runtime_checkable
class MessageTransport(Protocol):
    """Exchange complete messages; TV2 provides real TLS/TCP implementation."""

    def send(self, message: Envelope) -> None: ...

    def receive(self, *, timeout: float | None = None) -> Envelope: ...

    def close(self) -> None: ...


class ProtocolCodec(Protocol):
    """Encode/decode one complete framed TCP message (TV2 TODO)."""

    def encode(self, message: Envelope) -> bytes: ...

    def decode(self, frame: bytes) -> Envelope: ...
