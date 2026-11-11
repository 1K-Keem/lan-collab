"""Real loopback UDP/TLS checks for the concurrent connection milestone."""

from concurrent.futures import ThreadPoolExecutor
import socket
import ssl
import tempfile
from threading import Thread
from time import monotonic, sleep
import unittest

from lancollab.client.discovery import DiscoveryClient
from lancollab.common.devcerts import generate_dev_certs
from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.connections import ConnectionServer


class ConnectionServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.certs = generate_dev_certs(cls.tmp.name)
        cls.context = create_server_context(str(cls.certs.server_cert), str(cls.certs.server_key))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def start(self, **kwargs):
        server = ConnectionServer(self.context, port=0, udp_port=0, **kwargs)
        thread = Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.close)
        return server, thread

    def connect(self, server):
        transport = SocketTransport.connect_verified(
            "127.0.0.1", server.port, trusted_cert=str(self.certs.server_cert)
        )
        self.addCleanup(transport.close)
        return transport

    def wait_count(self, server, count):
        deadline = monotonic() + 3
        while server.active_connections != count and monotonic() < deadline:
            sleep(0.01)
        self.assertEqual(server.active_connections, count)

    def test_32_clients_discovery_capacity_and_reuse(self):
        server, thread = self.start()
        with ThreadPoolExecutor(max_workers=32) as pool:
            clients = list(pool.map(lambda _: self.connect(server), range(32)))
        self.wait_count(server, 32)
        for client in clients:
            with self.assertRaises(TimeoutError):
                client.receive(timeout=0.01)  # All remain connected simultaneously.
        with DiscoveryClient(udp_port=server.udp_port) as discovery:
            found = discovery.discover(broadcast_host="127.0.0.1")
            self.assertEqual(found.tcp_port, server.port)
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as excess:
            excess.settimeout(2)
            self.assertEqual(excess.recv(1), b"")
        clients[0].close()
        self.wait_count(server, 31)
        self.connect(server)
        self.wait_count(server, 32)
        server.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(server._udp_thread.is_alive())
        self.wait_count(server, 0)

    def test_stalled_handshake_does_not_block_accept_or_discovery(self):
        server, _ = self.start(handshake_timeout=0.5)
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as silent:
            client = self.connect(server)
            with DiscoveryClient(udp_port=server.udp_port) as discovery:
                self.assertIsNotNone(discovery.discover(broadcast_host="127.0.0.1"))
            silent.settimeout(2)
            self.assertEqual(silent.recv(1), b"")
            self.wait_count(server, 1)
            client.close()

    def test_wrong_credentials_get_auth_result_not_disconnect(self):
        import pathlib

        db = pathlib.Path(tempfile.mkdtemp()) / "accounts.db"
        accounts = AccountService(db)
        self.addCleanup(accounts.close)
        server, _ = self.start(accounts=accounts, handler=CollabServer(accounts))
        client = self.connect(server)
        client.send(
            Envelope(
                "AUTH_REQUEST", "auth-1", {"username": "alice", "password": "synthetic-test-only"}
            )
        )
        reply = client.receive(timeout=2)
        self.assertEqual(reply.type, "AUTH_RESULT")
        self.assertEqual(reply.payload, {"ok": False, "error_code": "AUTH_FAILED"})
        self.wait_count(server, 1)  # connection stays open; only the auth failed

    def test_udp_bind_failure_fails_startup(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
            busy.bind(("", 0))
            with self.assertRaises(OSError):
                ConnectionServer(self.context, port=0, udp_port=busy.getsockname()[1])

    def test_idle_peer_is_closed_and_slot_released(self):
        server, _ = self.start(first_message_timeout=0.2)
        client = self.connect(server)
        with self.assertRaises(ConnectionError):
            client.receive(timeout=2)
        self.wait_count(server, 0)

    def test_rejected_cert_reports_reason_releases_slots_and_good_client_still_connects(self):
        server, _ = self.start(max_clients=1)
        with tempfile.TemporaryDirectory() as directory:
            wrong = generate_dev_certs(directory)
            with self.assertLogs("lancollab.server.connections", level="WARNING") as logs:
                for _ in range(3):
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        SocketTransport.connect_verified(
                            "127.0.0.1", server.port, trusted_cert=str(wrong.server_cert)
                        )
                    self.wait_count(server, 0)
            self.assertEqual(len(logs.output), 3)
            self.assertTrue(all("TLS handshake failed" in line for line in logs.output))
            self.assertTrue(all("UNKNOWN_CA" in line for line in logs.output))
        self.connect(server)
        self.wait_count(server, 1)

    def test_plaintext_connection_logs_tls_reason(self):
        server, _ = self.start()
        with self.assertLogs("lancollab.server.connections", level="WARNING") as logs:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as raw:
                raw.sendall(b"GET / HTTP/1.0\r\n\r\n")
                raw.settimeout(2)
                try:
                    self.assertEqual(raw.recv(1), b"")
                except ConnectionResetError:
                    pass
            self.wait_count(server, 0)
        self.assertIn("TLS handshake failed", logs.output[0])
        self.assertIn("HTTP_REQUEST", logs.output[0])
