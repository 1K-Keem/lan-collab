"""TLS helpers: verified server identity, bounded handshakes, no ``CERT_NONE`` bypass.

The server presents a certificate whose SAN contains the fixed DNS name
``lan-collab.local``. Clients connect to whatever IP they discovered or were given but
verify the certificate against ``SERVER_HOSTNAME``, so DHCP address changes never require
a new certificate. Trust is anchored in a directly trusted server certificate
(``--trusted-cert``); the operating system trust store is never modified.
"""

from __future__ import annotations

import os
import selectors
import socket
import ssl
import time
from collections.abc import Mapping
from pathlib import Path

SERVER_HOSTNAME = "lan-collab.local"
HANDSHAKE_TIMEOUT = 5.0  # seconds, absolute, protocol-spec section 7

# The public self-signed server certificate shipped with the client (see .gitignore).
# so every copy of the app carries its own trust anchor; it never contains a private key.
BUNDLED_SERVER_CERT = Path(__file__).resolve().parents[1] / "client" / "server-public.pem"
TRUST_ENV_VAR = "LANCOLLAB_TRUSTED_CERT"
_PRIVATE_KEY_MARKER = "PRIVATE KEY-----"


def validate_trusted_cert(path: str | Path) -> str:
    """Return ``path`` as a string after checking it is a loadable, *public* certificate file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise FileNotFoundError(f"Trusted server certificate not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read certificate file {path}: {exc.__class__.__name__}") from exc
    if _PRIVATE_KEY_MARKER in text:
        raise ValueError(
            f"{path} contains a private key; give clients only the public server-public.pem"
        )
    try:
        ssl.create_default_context(cafile=str(path))
    except ssl.SSLError as exc:
        raise ValueError(f"{path} is not a valid PEM certificate file") from exc
    return str(path)


def resolve_trusted_cert(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    bundled: Path | None = None,
) -> str:
    """Resolve --trusted-cert, LANCOLLAB_TRUSTED_CERT, then bundled server-public.pem.

    Missing trust is an error: never fall back to unrelated system trust roots.
    """
    env = os.environ if environ is None else environ
    if explicit:
        return validate_trusted_cert(explicit)
    if env.get(TRUST_ENV_VAR):
        return validate_trusted_cert(env[TRUST_ENV_VAR])
    candidate = BUNDLED_SERVER_CERT if bundled is None else bundled
    if candidate.is_file():
        return validate_trusted_cert(candidate)
    raise FileNotFoundError(
        "No trusted server certificate; use --trusted-cert or bundle server-public.pem"
    )


def create_server_context(
    cert_file: str, key_file: str, *, key_password: str | None = None
) -> ssl.SSLContext:
    """TLS 1.2+ server context presenting ``cert_file`` (leaf, optionally with chain)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file, password=key_password)
    return context


def create_client_context(trusted_cert: str | None = None) -> ssl.SSLContext:
    """Trust the provisioned server certificate; retain hostname and validity checks."""
    trusted_cert = resolve_trusted_cert(trusted_cert)
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=trusted_cert)
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
