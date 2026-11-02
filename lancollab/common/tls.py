"""TLS helpers: verified server identity, bounded handshakes, no ``CERT_NONE`` bypass.

The server presents a certificate whose SAN contains the fixed DNS name
``lan-collab.local``. Clients connect to whatever IP they discovered or were given but
verify the certificate against ``SERVER_HOSTNAME``, so DHCP address changes never require
a new certificate. Trust is anchored in an explicit CA file (``--ca-file``); the operating
system trust store is never modified.
"""

from __future__ import annotations

import selectors
import socket
import ssl
import time

SERVER_HOSTNAME = "lan-collab.local"
HANDSHAKE_TIMEOUT = 5.0  # seconds, absolute, protocol-spec section 7


def create_server_context(
    cert_file: str, key_file: str, *, key_password: str | None = None
) -> ssl.SSLContext:
    """TLS 1.2+ server context presenting ``cert_file`` (leaf, optionally with chain)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file, password=key_password)
    return context


def create_client_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Verifying client context. With ``ca_file`` only that CA is trusted (pinned CA)."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _wait(sock: socket.socket, *, read: bool, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("TLS handshake timed out")
    with selectors.DefaultSelector() as selector:
        selector.register(sock, selectors.EVENT_READ if read else selectors.EVENT_WRITE)
        if not selector.select(remaining):
            raise TimeoutError("TLS handshake timed out")


def _handshake(tls: ssl.SSLSocket, deadline: float) -> None:
    """Drive the handshake non-blockingly so the *total* time is bounded, not each read."""
    tls.setblocking(False)
    while True:
        try:
            tls.do_handshake()
            return
        except ssl.SSLWantReadError:
            _wait(tls, read=True, deadline=deadline)
        except ssl.SSLWantWriteError:
            _wait(tls, read=False, deadline=deadline)


def _tune(sock: socket.socket) -> None:
    try:  # small control frames (PING/PONG, ACK) must not wait for Nagle
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def accept_tls(
    context: ssl.SSLContext, raw_sock: socket.socket, *, timeout: float = HANDSHAKE_TIMEOUT
) -> ssl.SSLSocket:
    """Server-side handshake with an absolute deadline. Run in a worker, never the accept loop.

    On any failure ``raw_sock`` is closed and the exception (``ssl.SSLError``,
    ``TimeoutError`` or ``OSError``) propagates.
    """
    deadline = time.monotonic() + timeout
    tls: ssl.SSLSocket | None = None
    try:
        _tune(raw_sock)
        raw_sock.setblocking(False)
        tls = context.wrap_socket(raw_sock, server_side=True, do_handshake_on_connect=False)
        _handshake(tls, deadline)
        tls.setblocking(True)
        return tls
    except BaseException:
        (tls or raw_sock).close()
        raise


def connect_tls(
    host: str,
    port: int,
    context: ssl.SSLContext,
    *,
    server_hostname: str = SERVER_HOSTNAME,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> ssl.SSLSocket:
    """Connect and verify the server certificate against ``server_hostname``.

    ``timeout`` bounds TCP connect plus TLS handshake together. An untrusted or mismatched
    certificate raises ``ssl.SSLCertVerificationError``; there is no insecure fallback.
    """
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("refusing to connect with certificate verification disabled")
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((host, port), timeout=timeout)
    tls: ssl.SSLSocket | None = None
    try:
        _tune(raw)
        raw.setblocking(False)
        tls = context.wrap_socket(
            raw, server_hostname=server_hostname, do_handshake_on_connect=False
        )
        _handshake(tls, deadline)
        tls.setblocking(True)
        return tls
    except BaseException:
        (tls or raw).close()
        raise
