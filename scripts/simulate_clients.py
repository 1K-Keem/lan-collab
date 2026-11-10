"""Trace real client discovery, TCP/TLS and cleanup against a running server.

No server is started by this script. Default: one client discovers the server.
Use --clients 20 for concurrent connections; --host skips broadcast discovery.
--send-auth demonstrates the current server's unimplemented authentication boundary
using synthetic credentials only, and expects a disconnect, never a successful login.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from pathlib import Path
import socket
from threading import Lock
from time import monotonic

from lancollab.client.discovery import DiscoveryClient, ServerEndpoint
from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import SERVER_HOSTNAME, create_client_context, resolve_trusted_cert

_PRINT_LOCK = Lock()
_STARTED = monotonic()


def report(client, text):
    with _PRINT_LOCK:
        print(f"[{monotonic() - _STARTED:6.2f}s] [{client}] {text}", flush=True)


def connect_client(index, args, trusted_cert):
    name = f"client-{index:02d}"
    raw = tls = None
    try:
        if args.host:
            endpoint = ServerEndpoint(args.host, args.port, args.server_name, False)
            report(name, f"DISCOVERY skipped: using {endpoint.host}:{endpoint.tcp_port}")
        else:
            with DiscoveryClient(udp_port=args.udp_port) as discovery:
                report(
                    name,
                    f"UDP DISCOVER -> {args.discovery_address}:{args.udp_port}; instance_id={discovery.instance_id}",
                )
                endpoint = discovery.discover(broadcast_host=args.discovery_address)
            if endpoint is None:
                raise ConnectionError(
                    "No valid DISCOVER_REPLY within 1.5s; check UDP responder or use --host"
                )
            report(
                name,
                f"UDP reply validated: source={endpoint.host}, TCP port={endpoint.tcp_port}, advertised name={endpoint.server_name!r}",
            )

        context = create_client_context(trusted_cert)
        report(name, f"TCP connect -> {endpoint.host}:{endpoint.tcp_port}")
        raw = socket.create_connection((endpoint.host, endpoint.tcp_port), timeout=5)
        report(
            name,
            f"TCP connected: local={raw.getsockname()}, peer={raw.getpeername()} (OS performed TCP handshake)",
        )
        report(name, f"TLS handshake: require trusted certificate + hostname {args.server_name!r}")
        tls = context.wrap_socket(raw, server_hostname=args.server_name)
        cipher = tls.cipher()
        fingerprint = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
        report(
            name,
            f"TLS VERIFIED: version={tls.version()}, cipher={cipher[0]}; certificate SHA-256={fingerprint}",
        )
        # Fingerprint is diagnostic only; ssl performed trust/hostname validation.
        transport = SocketTransport(tls, "client")
        return name, transport
    except (OSError, ValueError) as error:
        if tls is not None:
            tls.close()
        if raw is not None:
            raw.close()
        report(name, f"FAILED {type(error).__name__}: {error}")
        return name, None


def observe_client(name, transport, args):
    try:
        report(name, f"Keeping TLS connection open for {args.hold:.1f}s; no application data sent")
        try:
            event = transport.receive(timeout=args.hold)
        except TimeoutError:
            if transport.closed:
                raise ConnectionError("Transport closed while waiting")
        else:
            raise ValueError(f"Unexpected server event during idle test: {event.type}")
        report(name, "Connection remained open during hold period")
        if args.send_auth:
            request_id = f"demo-auth-{name}"
            transport.send(
                Envelope(
                    "AUTH_REQUEST",
                    request_id,
                    {
                        "username": name.replace("-", "_"),
                        "password": "synthetic-demo-not-a-real-password",
                    },
                )
            )
            report(name, f"Sent AUTH_REQUEST request_id={request_id} using synthetic credentials")
            try:
                event = transport.receive(timeout=3)
            except ConnectionError:
                report(
                    name,
                    "Server disconnected: expected for current milestone; authentication is NOT implemented",
                )
            else:
                raise ValueError(
                    f"Expected milestone disconnect; received {event.type}. This script does not test a full auth server"
                )
        return True
    except (OSError, ValueError) as error:
        report(name, f"FAILED during observation: {type(error).__name__}: {error}")
        return False
    finally:
        transport.close()
        report(name, "CLOSED locally; server should release this connection's worker slot")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--host", help="Explicit server IP; skips UDP discovery")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--udp-port", type=int, default=40001)
    parser.add_argument(
        "--discovery-address",
        default="255.255.255.255",
        help="UDP broadcast destination; 127.0.0.1 isolates same-machine tests",
    )
    parser.add_argument("--server-name", default=SERVER_HOSTNAME)
    parser.add_argument("--trusted-cert")
    parser.add_argument(
        "--hold", type=float, default=2.0, help="Idle observation seconds (0 < hold <= 5)"
    )
    parser.add_argument(
        "--send-auth",
        action="store_true",
        help="Send synthetic AUTH_REQUEST; expect milestone disconnect",
    )
    args = parser.parse_args()
    if not 1 <= args.clients <= 64:
        parser.error("--clients must be between 1 and 64 (server default capacity is 32)")
    if not 0 < args.hold <= 5:
        parser.error("--hold must be greater than 0 and at most 5 seconds")
    if not all(1 <= port <= 65535 for port in (args.port, args.udp_port)):
        parser.error("Ports must be between 1 and 65535")
    try:
        trusted_cert = resolve_trusted_cert(args.trusted_cert)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Trust configuration failed: {error}\n")
    report("test", f"Trusted certificate: {Path(trusted_cert).resolve()}")
    report("test", f"Starting {args.clients} clients concurrently; TLS verification stays enabled")
    report(
        "test", "Trace shows socket/TLS API results, not a packet capture of SYN or TLS messages"
    )
    connections = []
    passed = 0
    try:
        with ThreadPoolExecutor(max_workers=args.clients) as pool:
            futures = [
                pool.submit(connect_client, i, args, trusted_cert)
                for i in range(1, args.clients + 1)
            ]
            for future in as_completed(futures):
                name, transport = future.result()
                if transport is not None:
                    connections.append((name, transport))
            report(
                "test",
                f"TLS established for {len(connections)}/{args.clients}; observing them concurrently",
            )
            observations = [
                pool.submit(observe_client, name, transport, args)
                for name, transport in connections
            ]
            for future in as_completed(observations):
                passed += int(future.result())
    finally:
        for _, transport in connections:
            transport.close()
    report(
        "test",
        f"RESULT: {passed}/{args.clients} clients completed the requested checks; {args.clients - passed} failed",
    )
    report(
        "test",
        "This verifies discovery/connections only, not login, chat, files or server thread counts",
    )
    return 0 if passed == args.clients else 1


if __name__ == "__main__":
    raise SystemExit(main())
