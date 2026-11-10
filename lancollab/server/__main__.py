"""Concurrent TCP/TLS and UDP discovery server; account authentication is not implemented."""

import argparse
import logging

from lancollab.common.tls import create_server_context
from lancollab.server.connections import ConnectionServer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--udp-port", type=int, default=40001)
    parser.add_argument("--max-clients", type=int, default=32)
    parser.add_argument("--cert", default="certs/direct-trust/server-public.pem")
    parser.add_argument("--key", default="certs/direct-trust/server-secret.pem")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(threadName)s: %(message)s")
    try:
        context = create_server_context(args.cert, args.key)
        server = ConnectionServer(
            context, port=args.port, udp_port=args.udp_port, max_clients=args.max_clients
        )
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Server failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
