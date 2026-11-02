"""Verified TLS: trusted/untrusted/mismatched certificates, handshake bounds, encrypted bytes."""

import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import (
    SERVER_HOSTNAME,
    accept_tls,
    connect_tls,
    create_client_context,
    create_server_context,
)

try:
    from lancollab.common.devcerts import generate_dev_certs

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

SECRET = "TOP-SECRET-PAYLOAD-0123456789"


class Listener:
    """Test server: accepts connections in a worker thread and records handshake failures."""

    def __init__(self, context: ssl.SSLContext, handshake_timeout: float = 2.0) -> None:
        self.context = context
        self.handshake_timeout = handshake_timeout
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.errors: list[BaseException] = []
        self.received: list[Envelope] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        try:
            transport = SocketTransport.accept_verified(
                self.context, raw, timeout=self.handshake_timeout
            )
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)
            return
        try:
            message = transport.receive(timeout=3)
            self.received.append(message)
            transport.send(Envelope("CHAT_ACK", message.request_id, {"status": "queued"}))
        except Exception as exc:  # noqa: BLE001
            self.errors.append(exc)
        finally:
            transport.close()

    def close(self) -> None:
        self.sock.close()


@unittest.skipUnless(HAVE_CRYPTO, "needs the developer-only 'cryptography' package")
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.good = generate_dev_certs(base / "good")
        cls.other_ca = generate_dev_certs(base / "other")  # unrelated CA
        cls.wrong_name = generate_dev_certs(base / "wrong", hostnames=("other.example",))
        cls.ctx = create_server_context(str(cls.good.server_cert), str(cls.good.server_key))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def listener(self, certs=None, **kwargs) -> Listener:
        certs = certs or self.good
        ctx = create_server_context(str(certs.server_cert), str(certs.server_key))
        server = Listener(ctx, **kwargs)
        self.addCleanup(server.close)
        return server

    def test_trusted_certificate_connects_by_ip_and_exchanges_messages(self) -> None:
        server = self.listener()
        transport = SocketTransport.connect_verified(
            "127.0.0.1", server.port, str(self.good.ca_cert)
        )
        with transport:
            self.assertIn(transport.tls_version, ("TLSv1.2", "TLSv1.3"))
            transport.send(Envelope("CHAT_GLOBAL", "r1", {"text": "hello"}))
            reply = transport.receive(timeout=3)
        self.assertEqual(reply.payload, {"status": "queued"})
        self.assertEqual(server.received[0].payload["text"], "hello")
        self.assertEqual(server.errors, [])

    def test_untrusted_ca_is_rejected(self) -> None:
        server = self.listener()
        with self.assertRaises(ssl.SSLCertVerificationError):
            SocketTransport.connect_verified("127.0.0.1", server.port, str(self.other_ca.ca_cert))

    def test_certificate_for_another_hostname_is_rejected(self) -> None:
        server = self.listener(self.wrong_name)
        with self.assertRaises(ssl.SSLCertVerificationError):
            SocketTransport.connect_verified("127.0.0.1", server.port, str(self.wrong_name.ca_cert))

    def test_hostname_override_is_verified_not_the_ip(self) -> None:
        server = self.listener()
        ctx = create_client_context(str(self.good.ca_cert))
        with self.assertRaises(ssl.SSLCertVerificationError):
            connect_tls("127.0.0.1", server.port, ctx, server_hostname="evil.example")

    def test_insecure_client_context_is_refused(self) -> None:
        ctx = create_client_context(str(self.good.ca_cert))
        ctx.check_hostname = False
        with self.assertRaises(ValueError):
            connect_tls("127.0.0.1", 1, ctx)
        ctx = create_client_context(str(self.good.ca_cert))
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with self.assertRaises(ValueError):
            connect_tls("127.0.0.1", 1, ctx)

    def test_contexts_enforce_modern_tls(self) -> None:
        client = create_client_context(str(self.good.ca_cert))
        self.assertEqual(client.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client.check_hostname)
        self.assertGreaterEqual(client.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertGreaterEqual(self.ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(SERVER_HOSTNAME, "lan-collab.local")

    def test_silent_client_times_out_in_the_handshake_worker(self) -> None:
        server = self.listener(handshake_timeout=0.5)
        started = time.monotonic()
        silent = socket.create_connection(("127.0.0.1", server.port))
        self.addCleanup(silent.close)
        deadline = started + 3
        while not server.errors and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.errors, "handshake never timed out")
        self.assertIsInstance(server.errors[0], TimeoutError)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_trickled_handshake_cannot_extend_the_absolute_deadline(self) -> None:
        server = self.listener(handshake_timeout=0.6)
        sock = socket.create_connection(("127.0.0.1", server.port))
        self.addCleanup(sock.close)
        started = time.monotonic()

        def drip() -> None:
            try:
                for _ in range(40):  # one junk byte every 0.1s would defeat per-read timeouts
                    sock.sendall(b"\x16")
                    time.sleep(0.1)
            except OSError:
                pass

        threading.Thread(target=drip, daemon=True).start()
        while not server.errors and time.monotonic() - started < 3:
            time.sleep(0.05)
        self.assertTrue(server.errors)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_plaintext_client_cannot_talk_to_tls_server(self) -> None:
        server = self.listener()
        with socket.create_connection(("127.0.0.1", server.port)) as plain:
            plain.sendall(b'{"version":1}\n' * 4)
            plain.settimeout(2)
            try:
                data = plain.recv(100)
            except (OSError, TimeoutError):
                data = b""
        self.assertNotIn(b"queued", data)
        deadline = time.monotonic() + 3
        while not server.errors and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.errors)
        self.assertEqual(server.received, [])

    def test_wire_bytes_do_not_contain_plaintext(self) -> None:
        """Capture everything on the TCP link with a relay and prove it is encrypted."""
        server = self.listener()
        captured = bytearray()
        relay = socket.socket()
        relay.bind(("127.0.0.1", 0))
        relay.listen(1)
        self.addCleanup(relay.close)

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while data := src.recv(4096):
                    captured.extend(data)
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    s.close()

        def run_relay() -> None:
            downstream, _ = relay.accept()
            upstream = socket.create_connection(("127.0.0.1", server.port))
            threading.Thread(target=pump, args=(downstream, upstream), daemon=True).start()
            pump(upstream, downstream)

        threading.Thread(target=run_relay, daemon=True).start()
        with SocketTransport.connect_verified(
            "127.0.0.1", relay.getsockname()[1], str(self.good.ca_cert)
        ) as transport:
            transport.send(Envelope("CHAT_GLOBAL", "r1", {"text": SECRET}))
            transport.receive(timeout=3)
        self.assertEqual(server.received[0].payload["text"], SECRET)  # server did get it
        self.assertGreater(len(captured), 100)
        self.assertNotIn(SECRET.encode(), bytes(captured))
        self.assertNotIn(b"CHAT_GLOBAL", bytes(captured))

    def test_accept_tls_closes_socket_on_failure(self) -> None:
        a, b = socket.socketpair()
        b.close()
        with self.assertRaises((OSError, ssl.SSLError, TimeoutError)):
            accept_tls(self.ctx, a, timeout=0.5)
        self.assertEqual(a.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
