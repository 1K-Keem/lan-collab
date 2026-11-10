"""UDP discovery responder; discovery does not authenticate the server."""

import json
import logging
import socket
from threading import Event

from lancollab.common.tls import SERVER_HOSTNAME
from lancollab.common.udp_validation import (
    UDP_MAX_BYTES,
    reject_duplicate_keys,
    validate_datagram_size,
    validate_udp_message,
)


def bind_discovery_socket(udp_port: int) -> socket.socket:
    """Bind synchronously so startup reports port errors before accepting clients."""
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind(("", udp_port))
        udp.settimeout(0.2)
        return udp
    except BaseException:
        udp.close()
        raise


def serve_discovery(tcp_port=40000, udp_port=40001, *, stop=None, udp_socket=None):
    """Respond until stopped. A supplied socket is owned and closed by the caller."""
    stop = stop if stop is not None else Event()
    owns_socket = udp_socket is None
    udp = bind_discovery_socket(udp_port) if owns_socket else udp_socket
    try:
        while not stop.is_set():
            try:
                raw, client_address = udp.recvfrom(UDP_MAX_BYTES + 1)
            except TimeoutError:
                continue
            except OSError:
                if not stop.is_set():
                    raise
                break
            try:
                validate_datagram_size(raw)
                request = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
                validate_udp_message(request)
                if request["type"] != "DISCOVER":
                    continue
                reply = {
                    "version": 1,
                    "type": "DISCOVER_REPLY",
                    "instance_id": request["instance_id"],
                    "server_name": SERVER_HOSTNAME,
                    "tcp_port": tcp_port,
                }
                validate_udp_message(reply)
                data = json.dumps(reply).encode("utf-8")
                validate_datagram_size(data)
            except (ValueError, UnicodeDecodeError):
                continue
            try:
                udp.sendto(data, client_address)
            except OSError:
                if not stop.is_set():
                    logging.warning("Could not send discovery reply to %s", client_address)
    finally:
        if owns_socket:
            udp.close()


if __name__ == "__main__":
    try:
        print("UDP discovery listening on port 40001", flush=True)
        serve_discovery()
    except KeyboardInterrupt:
        pass
