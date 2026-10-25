"""TV2 TODO: raw TCP/TLS transport and length-prefixed JSON codec.

Do not use this placeholder in production or as grading evidence.
"""

from .contracts import Envelope


class JsonFrameCodec:
    """Implements ProtocolCodec after TV2 completes Issue #3."""

    def encode(self, message: Envelope) -> bytes:
        raise NotImplementedError("TV2: encode big-endian length + JSON")

    def decode(self, frame: bytes) -> Envelope:
        raise NotImplementedError("TV2: validate length, parse JSON envelope")


class SocketTransport:
    """Implements MessageTransport over a verified TLS socket (TV2)."""

    def __init__(self, tls_socket: object) -> None:
        self._socket = tls_socket

    def send(self, message: Envelope) -> None:
        raise NotImplementedError("TV2: serialize and send complete framed message")

    def receive(self, *, timeout: float | None = None) -> Envelope:
        raise NotImplementedError("TV2: receive exact framed message")

    def close(self) -> None:
        raise NotImplementedError("TV2: shutdown and close transport")
