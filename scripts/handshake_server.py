"""Accept one TCP/TLS handshake without application authentication."""

import argparse
import socket

from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--cert", default="certs/direct-trust/server.pem")
    parser.add_argument("--key", default="certs/direct-trust/server-key.pem")
    args = parser.parse_args()
    try:
        context = create_server_context(args.cert, args.key)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.host, args.port))
            listener.listen(1)
            print(f"Listening on {listener.getsockname()}", flush=True)
            raw, address = listener.accept()
            print(f"TCP accepted from {address}", flush=True)
            with raw:
                with SocketTransport.accept_verified(context, raw) as transport:
                    print(f"TLS handshake completed: {transport.tls_version}", flush=True)
    except (OSError, ValueError) as error:
        print(f"Handshake failed: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
