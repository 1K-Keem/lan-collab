"""Raw TCP/TLS transport: exact-read framing, absolute deadlines, one serialized writer.

``SocketTransport`` implements :class:`lancollab.common.contracts.MessageTransport` on top of
a connected (normally TLS) stream socket:

* the socket is switched to non-blocking mode; every deadline is absolute and measured with
  ``time.monotonic()`` (a trickle of bytes cannot extend it);
* ``receive`` reads exactly the 4-byte prefix, validates the length *before* reading or
  allocating the body, then reads exactly the body, so fragmented and coalesced TCP reads
  both work;
* ``send`` serializes whole frames (no interleaving between threads) and enforces the
  5 second complete-frame write deadline;
* a reader thread and a writer thread may use one transport concurrently; individual
  non-blocking socket calls are serialized with a short lock because OpenSSL objects are
  not safe for concurrent use.
"""

from __future__ import annotations

import selectors
import socket
import ssl
import threading
import time
from typing import Literal

from .codec import JsonFrameCodec, LENGTH_PREFIX, ProtocolError, Role, check_frame_length
from .contracts import Envelope
from .tls import (
    HANDSHAKE_TIMEOUT,
    SERVER_HOSTNAME,
    accept_tls,
    connect_tls,
    create_client_context,
)

__all__ = ["JsonFrameCodec", "SocketTransport"]

FRAME_READ_TIMEOUT = 10.0  # absolute, from the first prefix byte of a frame
FRAME_WRITE_TIMEOUT = 5.0  # absolute, for one complete frame
_POLL_SECONDS = 0.2  # how often blocked waiters notice close()
_READ_CHUNK = 65536

_Want = Literal["read", "write"]


class SocketTransport:
    """Message transport over a connected stream socket (use TLS in production)."""

    def __init__(
        self,
        sock: socket.socket,
        role: Role,
        *,
        codec: JsonFrameCodec | None = None,
        write_timeout: float = FRAME_WRITE_TIMEOUT,
        frame_read_timeout: float = FRAME_READ_TIMEOUT,
    ) -> None:
        self._sock = sock
        self._sock.setblocking(False)
        self._codec = codec or JsonFrameCodec(role)
        self._write_timeout = write_timeout
        self._frame_read_timeout = frame_read_timeout
        self._io_lock = threading.Lock()  # one non-blocking socket call at a time
        self._send_lock = threading.Lock()  # one whole frame at a time
        self._recv_lock = threading.Lock()  # one reader at a time
        self._rx = bytearray()
        self._closed = threading.Event()
        try:
            self.peer_address: tuple[str, int] | None = sock.getpeername()[:2]
        except OSError:
            self.peer_address = None

    # ------------------------------------------------------------------ constructors
    @classmethod
    def connect_verified(
        cls,
        host: str,
        port: int,
        trusted_cert: str | None = None,
        *,
        server_hostname: str = SERVER_HOSTNAME,
        timeout: float = HANDSHAKE_TIMEOUT,
        context: ssl.SSLContext | None = None,
    ) -> SocketTransport:
        """Client side: TCP connect + verified TLS (certificate and hostname checked)."""
        ctx = context or create_client_context(trusted_cert)
        tls = connect_tls(host, port, ctx, server_hostname=server_hostname, timeout=timeout)
        return cls(tls, "client")

    @classmethod
    def accept_verified(
        cls,
        context: ssl.SSLContext,
        raw_sock: socket.socket,
        *,
        timeout: float = HANDSHAKE_TIMEOUT,
    ) -> SocketTransport:
        """Server side: bounded TLS handshake for an accepted socket (call from a worker)."""
        return cls(accept_tls(context, raw_sock, timeout=timeout), "server")

    # --------------------------------------------------------------------- properties
    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def tls_version(self) -> str | None:
        return self._sock.version() if isinstance(self._sock, ssl.SSLSocket) else None

    # ------------------------------------------------------------------ socket plumbing
    def _wait(self, want: _Want, deadline: float | None) -> bool:
        """Block until the socket is ready for ``want``; ``False`` when the deadline passes."""
        event = selectors.EVENT_READ if want == "read" else selectors.EVENT_WRITE
        while True:
            if self._closed.is_set():
                raise ConnectionError("transport closed")
            if deadline is None:
                slice_seconds = _POLL_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                slice_seconds = min(_POLL_SECONDS, remaining)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(self._sock, event)
                    if selector.select(slice_seconds):
                        return True
            except (OSError, ValueError) as exc:  # socket closed under us
                raise ConnectionError("transport closed") from exc

    def _recv_some(self) -> tuple[bytes, _Want]:
        """One non-blocking read attempt: ``(data, _)`` or ``(b"", what_to_wait_for)``."""
        with self._io_lock:
            if self._closed.is_set():
                raise ConnectionError("transport closed")
            try:
                data = self._sock.recv(_READ_CHUNK)
            except ssl.SSLWantReadError:
                return b"", "read"
            except ssl.SSLWantWriteError:
                return b"", "write"
            except BlockingIOError:
                return b"", "read"
            except (ssl.SSLZeroReturnError, ssl.SSLEOFError) as exc:
                raise ConnectionError("peer closed the connection") from exc
            except OSError as exc:
                raise ConnectionError(f"receive failed: {exc.__class__.__name__}") from exc
        if not data:
            raise ConnectionError("peer closed the connection")
        return data, "read"

    def _send_some(self, view: memoryview) -> tuple[int, _Want]:
        with self._io_lock:
            if self._closed.is_set():
                raise ConnectionError("transport closed")
            try:
                return self._sock.send(view), "write"
            except ssl.SSLWantWriteError:
                return 0, "write"
            except ssl.SSLWantReadError:
                return 0, "read"
            except BlockingIOError:
                return 0, "write"
            except OSError as exc:
                raise ConnectionError(f"send failed: {exc.__class__.__name__}") from exc

    def _fill(self, needed: int, deadline: float | None) -> bool:
        """Grow the receive buffer to ``needed`` bytes; ``False`` if the deadline passed."""
        while len(self._rx) < needed:
            data, want = self._recv_some()
            if data:
                self._rx += data
            elif not self._wait(want, deadline):
                return False
        return True

    # -------------------------------------------------------------------- public API
    def send(self, message: Envelope) -> None:
        """Validate, frame and write ``message`` as one uninterleaved frame."""
        view = memoryview(self._codec.encode(message))
        with self._send_lock:
            deadline = time.monotonic() + self._write_timeout
            sent = 0
            while sent < len(view):
                written, want = self._send_some(view[sent:])
                if written:
                    sent += written
                elif not self._wait(want, deadline):
                    self.close()  # a partial frame may be on the wire: stream is unusable
                    raise TimeoutError("frame write deadline exceeded")

    def receive(self, *, timeout: float | None = None, deadline: float | None = None) -> Envelope:
        """Return the next validated message.

        ``TimeoutError`` with nothing consumed means no frame started within ``timeout``
        (the transport stays usable). ``ConnectionError`` means EOF/reset/closed. A
        :class:`~lancollab.common.codec.FrameError` closes the transport; a
        :class:`~lancollab.common.codec.MessageError` leaves it open and in sync so the caller
        can reply with ``ERROR``. Optional absolute monotonic ``deadline`` caps
        both initial polling and partial-frame reads (e.g. the authentication deadline).
        """
        with self._recv_lock:
            first = None if timeout is None else time.monotonic() + timeout
            if deadline is not None:
                first = deadline if first is None else min(first, deadline)
            if not self._fill(1, first):
                raise TimeoutError("no frame available")
            frame_deadline = time.monotonic() + self._frame_read_timeout
            if deadline is not None:
                frame_deadline = min(frame_deadline, deadline)
            try:
                if not self._fill(LENGTH_PREFIX.size, frame_deadline):
                    raise self._frame_timeout()
                (length,) = LENGTH_PREFIX.unpack_from(self._rx)
                check_frame_length(length)  # before reading/allocating the body
                if not self._fill(LENGTH_PREFIX.size + length, frame_deadline):
                    raise self._frame_timeout()
            except ProtocolError:
                self.close()
                raise
            body = bytes(self._rx[LENGTH_PREFIX.size : LENGTH_PREFIX.size + length])
            del self._rx[: LENGTH_PREFIX.size + length]
            try:
                return self._codec.decode_body(body)
            except ProtocolError as exc:
                if exc.fatal:
                    self.close()
                raise

    def _frame_timeout(self) -> TimeoutError:
        self.close()
        return TimeoutError("frame read deadline exceeded")

    def close(self) -> None:
        """Idempotent; wakes blocked readers/writers within one poll interval."""
        if self._closed.is_set():
            return
        self._closed.set()
        with self._io_lock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass

    def __enter__(self) -> SocketTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
