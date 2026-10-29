"""TV3 CLI entry point; real verified TLS connector is an explicit TV2 boundary."""

import argparse
from pathlib import Path
import sys

from lancollab.client.cli import ConsoleSession, prompt_identity
from lancollab.client.discovery import DiscoveryClient, select_server
from lancollab.common.contracts import MessageTransport


def connect_verified_transport(
    *, host: str, port: int, server_name: str, ca_file: str
) -> MessageTransport:
    """TV2 must implement SocketTransport.connect_verified in Issue #3/#10.

    Do NOT silently fall back to unencrypted TCP or disable certificate checking.
    """
    from lancollab.common.socket_transport import SocketTransport

    connector = getattr(SocketTransport, "connect_verified", None)
    if not callable(connector):
        raise RuntimeError(
            "TV2 integration pending: implement SocketTransport.connect_verified"
            "(host, port, server_name, ca_file) with verified TLS before credentials."
        )
    return connector(host=host, port=port, server_name=server_name, ca_file=ca_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN Collab portable client (TV3)")
    parser.add_argument("--host", help="Explicit server IP/hostname (skips discovery)")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument(
        "--server-name", default="lan-collab.local", help="TLS verified DNS identity"
    )
    parser.add_argument("--ca-file", required=True, help="CA/certificate file for verified TLS")
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
    if not Path(args.ca_file).is_file():
        parser.error("CA/certificate file not found")
    try:
        # Request credentials BEFORE connecting to honor the server's auth deadline.
        username, password = prompt_identity()
        with DiscoveryClient(udp_port=args.udp_port) as discovery:
            found = discovery.discover() if args.discover and not args.host else None
            endpoint = select_server(found, host=args.host, port=args.port)
            # Manual IP does not bypass certificate verification of --server-name.
            transport = connect_verified_transport(
                host=endpoint.host,
                port=endpoint.tcp_port,
                server_name=args.server_name,
                ca_file=args.ca_file,
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
