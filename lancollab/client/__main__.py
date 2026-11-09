"""Client CLI entry point: resolve the server, verify TLS, then authenticate."""

import argparse
from pathlib import Path
import sys

from lancollab.client.cli import ConsoleSession, prompt_identity
from lancollab.client.discovery import DiscoveryClient, select_server
from lancollab.common.tls import resolve_trusted_cert
from lancollab.common.socket_transport import SocketTransport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN Collab portable client (TV3)")
    parser.add_argument("--host", help="Explicit server IP/hostname (skips discovery)")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument(
        "--server-name", default="lan-collab.local", help="TLS verified DNS identity"
    )
    parser.add_argument(
        "--trusted-cert",
        help="Trusted server certificate (default: LANCOLLAB_TRUSTED_CERT or bundled server-public.pem)",
    )
    parser.add_argument("--udp-port", type=int, default=40001)
    parser.add_argument(
        "--discover", action="store_true", help="Broadcast for server if --host omitted"
    )
    parser.add_argument("--downloads-dir", type=Path, default=Path("downloads"))
    args = parser.parse_args(argv)
    if not args.host and not args.discover:
        parser.error("Use --host or --discover (manual --host is always a fallback)")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid TCP port")
    try:
        trusted_cert = resolve_trusted_cert(args.trusted_cert)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        # Request credentials BEFORE connecting to honor the server's auth deadline.
        username, password = prompt_identity()
        with DiscoveryClient(udp_port=args.udp_port) as discovery:
            found = discovery.discover() if args.discover and not args.host else None
            endpoint = select_server(found, host=args.host, port=args.port)
            # Manual IP does not bypass certificate verification of --server-name.
            transport = SocketTransport.connect_verified(
                host=endpoint.host,
                port=endpoint.tcp_port,
                server_hostname=args.server_name,
                trusted_cert=trusted_cert,
            )
            shell = ConsoleSession(
                transport,
                downloads_dir=args.downloads_dir,
                udp=discovery,
                server_host=endpoint.host,
            )
            try:
                shell.authenticate(username, password)
                shell.run_commands()
            finally:
                shell.close()
    except (ConnectionError, OSError, RuntimeError, TimeoutError) as error:
        print(f"Client stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
