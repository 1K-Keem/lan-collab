"""Verify the server certificate without sending credentials or application messages."""

import argparse

from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import SERVER_HOSTNAME, resolve_trusted_cert


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--server-name", default=SERVER_HOSTNAME)
    parser.add_argument("--trusted-cert", help="Override the bundled server certificate")
    args = parser.parse_args()
    try:
        with SocketTransport.connect_verified(
            host=args.host,
            port=args.port,
            server_hostname=args.server_name,
            trusted_cert=resolve_trusted_cert(args.trusted_cert),
        ) as transport:
            print(f"Server verified. TLS version: {transport.tls_version}")
    except (OSError, ValueError) as error:
        print(f"Connection or certificate verification failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
