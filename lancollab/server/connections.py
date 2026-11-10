"""Bounded concurrent TCP/TLS milestone; account authentication is not implemented."""

import logging
import socket
import ssl
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic

from lancollab.common.codec import ProtocolError
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import HANDSHAKE_TIMEOUT
from lancollab.server.discovery import bind_discovery_socket, serve_discovery

LOG = logging.getLogger(__name__)


class ConnectionServer:
    """One accept loop, one UDP thread, at most max_clients connection workers.

    Capacity includes peers still handshaking. After TLS, wait up to 10 seconds for
    a first frame or disconnect. A frame is logged then the connection is closed:
    this milestone does not grant authentication or dispatch chat/file operations.
    """

    def __init__(
        self,
        context: ssl.SSLContext,
        *,
        port=40000,
        udp_port=40001,
        max_clients=32,
        handshake_timeout=HANDSHAKE_TIMEOUT,
        first_message_timeout=10.0,
    ):
        if type(max_clients) is not int or max_clients < 1:
            raise ValueError("max_clients must be a positive integer")
        if handshake_timeout <= 0 or first_message_timeout <= 0:
            raise ValueError("Timeouts must be positive")
        for value in (port, udp_port):
            if type(value) is not int or not 0 <= value <= 65535:
                raise ValueError("Ports must be integers in 0–65535")
        
        self.context = context
        self.max_clients = max_clients
        self.handshake_timeout = handshake_timeout
        self.first_message_timeout = first_message_timeout
        self.stop = Event()
        self._slots = BoundedSemaphore(max_clients)
        self._lock = Lock()
        self._workers = {}  # thread -> raw socket or verified transport
        self._udp_thread = None
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(("", port))
            self.listener.listen(max_clients)
            self.listener.settimeout(0.2)
            self.port = self.listener.getsockname()[1]
            self.udp = bind_discovery_socket(udp_port)
            self.udp_port = self.udp.getsockname()[1]
        except BaseException:
            self.listener.close()
            raise

    @property
    def active_connections(self):
        with self._lock:
            return len(self._workers)

    def _run_discovery(self):
        try:
            serve_discovery(self.port, stop=self.stop, udp_socket=self.udp)
        except OSError:
            LOG.exception("UDP responder failed; stopping the server")
            self.stop.set()

    def serve_forever(self):
        try:
            self._udp_thread = Thread(target=self._run_discovery, name="udp-discovery")
            self._udp_thread.start()
            LOG.info(
                "TCP port %s; UDP port %s; capacity %s", self.port, self.udp_port, self.max_clients
            )
            while not self.stop.is_set():
                try:
                    raw, address = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self.stop.is_set():
                        break
                    raise

                if not self._slots.acquire(blocking=False):
                    raw.close()  # No plaintext protocol error before TLS.
                    LOG.info("Connection capacity reached; rejected %s", address)
                    continue
                
                worker = Thread(target=self._handle_client, args=(raw, address), name=f"client-{address}")
                with self._lock:
                    if self.stop.is_set():
                        raw.close()
                        self._slots.release()
                        break
                    self._workers[worker] = raw
                    try:
                        worker.start()
                    except BaseException:
                        self._workers.pop(worker)
                        raw.close()
                        self._slots.release()
                        raise
        finally:
            self.close()

    def _handle_client(self, raw, address):
        from threading import current_thread

        worker = current_thread()
        transport = None
        try:
            transport = SocketTransport.accept_verified(self.context, raw, timeout=self.handshake_timeout)

            with self._lock:
                self._workers[worker] = transport

            LOG.info("TLS ready for %s: %s", address, transport.tls_version)
            deadline = monotonic() + self.first_message_timeout
            
            while not self.stop.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                try:
                    message = transport.receive(timeout=min(0.2, remaining))
                except TimeoutError:
                    if transport.closed:
                        break
                    continue

                LOG.info(
                    "Closing %s after %s: authentication/dispatch not implemented",
                    address,
                    message.type,
                )
                break
        except (OSError, ValueError, ProtocolError) as error:
            LOG.info("Connection %s ended: %s", address, type(error).__name__)
        finally:
            if transport is not None:
                transport.close()
            raw.close()
            with self._lock:
                self._workers.pop(worker, None)
            self._slots.release()

    def close(self):
        """Stop listeners and active peers; handshake workers have a bounded deadline."""
        self.stop.set()
        self.listener.close()
        self.udp.close()
        with self._lock:
            workers = list(self._workers.items())
        for _, connection in workers:
            connection.close()
        deadline = monotonic() + self.handshake_timeout + 1
        for worker, _ in workers:
            worker.join(timeout=max(0, deadline - monotonic()))
        if self._udp_thread is not None:
            self._udp_thread.join(timeout=1)
