"""Bounded concurrent TCP/TLS server; dispatches to CollabServer for auth."""

import logging
import socket
import ssl
import uuid
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic

from lancollab.common.codec import MessageError, ProtocolError, RequestIdTracker
from lancollab.common.contracts import Envelope, OutboundEvent, SessionIdentity
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import HANDSHAKE_TIMEOUT, tls_failure_detail
from lancollab.server.outbound import OutboundQueue
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.discovery import bind_discovery_socket, serve_discovery

LOG = logging.getLogger(__name__)


class ConnectionServer:
    """One accept loop, one UDP thread, at most max_clients connection workers.

    Capacity includes peers still handshaking. Each TLS connection has a reader
    and one bounded-queue writer. Dispatch and queue admission are serialized;
    socket writes never hold the shared application lock.
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
        ping_interval=10.0,
        accounts: AccountService | None = None,
        handler: CollabServer | None = None,
    ):
        if type(max_clients) is not int or max_clients < 1:
            raise ValueError("max_clients must be a positive integer")
        if handshake_timeout <= 0 or first_message_timeout <= 0 or ping_interval <= 0:
            raise ValueError("Timeouts must be positive")
        for value in (port, udp_port):
            if type(value) is not int or not 0 <= value <= 65535:
                raise ValueError("Ports must be integers in 0–65535")

        self.context = context
        self.max_clients = max_clients
        self.handshake_timeout = handshake_timeout
        self.first_message_timeout = first_message_timeout
        self.ping_interval = ping_interval
        self.accounts = accounts
        self.handler = (
            handler
            if handler is not None
            else (CollabServer(accounts) if accounts is not None else None)
        )
        self.stop = Event()
        self._slots = BoundedSemaphore(max_clients)
        self._lock = Lock()
        self._workers = {}  # thread -> raw socket or verified transport
        self._transports = {}  # session_id -> SocketTransport
        self._queues = {}
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

                worker = Thread(
                    target=self._handle_client, args=(raw, address), name=f"client-{address}"
                )
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

    def _deliver(self, events):
        """Called under handler.lock: admission is nonblocking and preserves event order."""
        pending = list(events)
        while pending:
            if self.handler is not None:
                purges = self.handler.pop_purges()
                if purges:
                    with self._lock:
                        queues = list(self._queues.values())
                    for queue in queues:
                        queue.purge(purges)
            event = pending.pop(0)
            sid = event.recipient.session_id
            with self._lock:
                queue = self._queues.get(sid)
                target = self._transports.get(sid)
            if queue is None or queue.closing:
                continue
            if queue.put(event.message):
                continue
            if event.message.type in {"FILE_CHUNK", "FILE_END"}:
                outcome = self.handler.transfers.fail_queue_full(
                    event.message.payload["transfer_id"]
                )
                pending.extend(self.handler.transfer_events(outcome))
            else:
                queue.close(discard=True)
                target.close()
                pending.extend(self.handler.close_session(sid))
        # Cancellation can purge without generating any forwarded events.
        if self.handler is not None:
            purges = self.handler.pop_purges()
            with self._lock:
                for queue in self._queues.values():
                    queue.purge(purges)

    def _write_client(self, transport, queue):
        try:
            while (message := queue.get()) is not None:
                transport.send(message)
                if self.handler is not None and message.type in {
                    "FILE_OFFER",
                    "FILE_ACCEPT",
                    "FILE_END",
                }:
                    self.handler.transfers.mark_written(
                        message.payload["transfer_id"], message.type
                    )
        except (OSError, ValueError, ProtocolError):
            LOG.info("Writer failed; closing receiving session")
        finally:
            queue.close(discard=True)
            transport.close()  # Unblocks the reader, which owns session cleanup.

    def _handle_client(self, raw, address):
        from threading import current_thread

        worker = current_thread()
        transport = None
        writer = None
        queue = OutboundQueue()
        tracker = RequestIdTracker()
        session = SessionIdentity(address[0], address[1], uuid.uuid4().hex)
        pending_pings = {}
        next_ping = monotonic() + self.ping_interval
        try:
            transport = SocketTransport.accept_verified(
                self.context, raw, timeout=self.handshake_timeout
            )
            with self._lock:
                self._workers[worker] = transport
                self._transports[session.session_id] = transport
                self._queues[session.session_id] = queue
            writer = Thread(
                target=self._write_client,
                args=(transport, queue),
                name=f"writer-{session.session_id}",
            )
            writer.start()
            LOG.info("TLS ready for %s: %s", address, transport.tls_version)
            deadline = monotonic() + self.first_message_timeout
            while not self.stop.is_set() and not queue.closing:
                authed = (
                    self.handler is not None
                    and self.handler.session_for(session.session_id) is not None
                )
                if not authed and monotonic() >= deadline:
                    break
                if self.handler is not None:
                    with self.handler.lock:
                        self._deliver(self.handler.transfer_events(self.handler.transfers.expire()))
                        if authed and monotonic() >= next_ping:
                            if len(pending_pings) >= 3:
                                break
                            rid, nonce = uuid.uuid4().hex, uuid.uuid4().hex
                            pending_pings[rid] = nonce
                            self._deliver(
                                [OutboundEvent(session, Envelope("PING", rid, {"nonce": nonce}))]
                            )
                            next_ping = monotonic() + self.ping_interval
                try:
                    message = transport.receive(timeout=0.2, deadline=None if authed else deadline)
                except MessageError as error:
                    # Semantically invalid frames still consume their request id.
                    try:
                        tracker.register(error.request_id)
                    except MessageError as duplicate:
                        error = duplicate
                    if not queue.put(error.to_error_envelope()):
                        break
                    continue
                except TimeoutError:
                    if transport.closed:
                        break
                    continue
                except (OSError, ValueError, ProtocolError):
                    break
                if message.type != "PONG":
                    try:
                        tracker.register(message.request_id)
                    except MessageError as error:
                        if not queue.put(error.to_error_envelope()):
                            break
                        continue
                if self.handler is None:
                    break
                with self.handler.lock:
                    if queue.closing or (not authed and monotonic() >= deadline):
                        break
                    if message.type == "PONG":
                        if (
                            authed
                            and pending_pings.get(message.request_id) == message.payload["nonce"]
                        ):
                            pending_pings.pop(message.request_id)
                            continue
                        events = [self.handler._error(session, message.request_id, "INVALID_STATE")]
                    else:
                        events = self.handler.handle_message(message, session)
                    updated = self.handler.session_for(session.session_id)
                    if updated is not None:
                        session = updated
                    self._deliver(events)
                    for sid in self.handler.pop_closed_sessions():
                        with self._lock:
                            victim = self._queues.get(sid)
                        if victim is not None:
                            victim.close()  # Writer drains final response, then closes.
        except ssl.SSLError as error:
            phase = "TLS handshake" if transport is None else "TLS session"
            LOG.warning("%s failed for %s: %s", phase, address, tls_failure_detail(error))
        except (OSError, ValueError, ProtocolError) as error:
            LOG.info("Connection %s ended: %s", address, type(error).__name__)
        except Exception:
            LOG.exception("Connection handler failed for %s", address)
        finally:
            if self.handler is not None:
                with self.handler.lock:
                    self._deliver(self.handler.close_session(session.session_id))
            queue.close()
            if writer is not None:
                writer.join(timeout=5)
            if transport is not None:
                transport.close()
            queue.close(discard=True)
            if writer is not None and writer.is_alive():
                writer.join(timeout=1)
            raw.close()
            with self._lock:
                self._workers.pop(worker, None)
                self._transports.pop(session.session_id, None)
                self._queues.pop(session.session_id, None)
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
