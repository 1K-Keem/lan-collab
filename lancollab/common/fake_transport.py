"""Deterministic in-memory TEST DOUBLE.

This adapter does NOT use sockets, TCP framing, UDP, TLS, or concurrency.
It exists solely so TV1 and TV3 can test their APIs independently of TV2.
"""

from collections import deque
from typing import Deque

from .contracts import Envelope


class FakeTransport:
    """Pair two endpoints and exchange Envelope objects, not wire bytes."""

    def __init__(self) -> None:
        self._inbox: Deque[Envelope] = deque()
        self._peer: FakeTransport | None = None
        self._closed = False

    @classmethod
    def pair(cls) -> tuple["FakeTransport", "FakeTransport"]:
        left, right = cls(), cls()
        left._peer = right
        right._peer = left
        return left, right

    def send(self, message: Envelope) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise BrokenPipeError("fake endpoint closed or unpaired")
        self._peer._inbox.append(message)

    def receive(self, *, timeout: float | None = None) -> Envelope:
        # Intentionally nonblocking for repeatable mock tests.
        # The timeout is part of the interface; this fake does not wait.
        del timeout
        if self._closed:
            raise ConnectionError("fake endpoint closed")
        if not self._inbox:
            raise TimeoutError("no pending fake message")
        return self._inbox.popleft()

    def close(self) -> None:
        self._closed = True
