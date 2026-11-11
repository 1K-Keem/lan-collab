"""Exercise the production relay, clients and bounded writers over verified TLS."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
import tempfile
import json
import struct
import unittest

from lancollab.client.application import ClientState, CollabClient
from lancollab.client.discovery import DiscoveryClient
from lancollab.common.contracts import Envelope
from lancollab.common.devcerts import generate_dev_certs
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.connections import ConnectionServer
from lancollab.server.outbound import OutboundQueue


class QueueTests(unittest.TestCase):
    def test_bounds_fairness_end_order_and_purge(self):
        queue = OutboundQueue(control_limit=8, file_limit=2)
        for i in range(8):
            self.assertTrue(queue.put(Envelope("PING", str(i), {"nonce": str(i)})))
        self.assertFalse(queue.put(Envelope("PING", "full", {"nonce": "full"})))
        for kind in ("FILE_CHUNK", "FILE_END"):
            self.assertTrue(queue.put(Envelope(kind, kind, {"transfer_id": "one"})))
        self.assertFalse(queue.put(Envelope("FILE_CHUNK", "full", {})))
        self.assertEqual([queue.get().type for _ in range(5)], ["PING"] * 4 + ["FILE_CHUNK"])
        self.assertEqual([queue.get().type for _ in range(5)], ["PING"] * 4 + ["FILE_END"])
        queue.put(Envelope("FILE_CHUNK", "purge", {"transfer_id": "one"}))
        queue.purge({"one"})
        queue.close()
        self.assertIsNone(queue.get())
        self.assertFalse(queue.put(Envelope("PING", "closed", {})))


class ProductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificate_dir = tempfile.TemporaryDirectory()
        cls.certs = generate_dev_certs(cls.certificate_dir.name)
        cls.context = create_server_context(str(cls.certs.server_cert), str(cls.certs.server_key))

    @classmethod
    def tearDownClass(cls):
        cls.certificate_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = AccountService(Path(self.tmp.name) / "accounts.db")
        self.addCleanup(self.accounts.close)
        self.handler = CollabServer(self.accounts)
        self.server = ConnectionServer(self.context, port=0, udp_port=0, handler=self.handler)
        thread = Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.close)

    def wait(self, condition, timeout=8):
        deadline = monotonic() + timeout
        while not condition() and monotonic() < deadline:
            sleep(0.01)
        self.assertTrue(condition(), "Condition did not become true")

    def transport(self):
        transport = SocketTransport.connect_verified(
            "127.0.0.1", self.server.port, trusted_cert=str(self.certs.server_cert)
        )
        self.addCleanup(transport.close)
        return transport

    def client(self, name, create=True):
        if create:
            self.accounts.create_account(name, "synthetic-password", status="active")
        client = CollabClient(
            self.transport(), auth_allowed=True, downloads_dir=Path(self.tmp.name) / name
        )
        stop = Event()
        thread = Thread(target=client.receive_loop, args=(stop,))
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(stop.set)
        self.addCleanup(client.close)
        client.login(name, "synthetic-password")
        self.wait(lambda: client.state == ClientState.AUTHENTICATED)
        return client

    def receive_kind(self, transport, kind):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            message = transport.receive(timeout=2)
            if message.type == kind:
                return message
        self.fail(f"No {kind}")

    def test_twenty_authenticated_clients_chat_during_file_transfer(self):
        for i in range(20):
            self.accounts.create_account(f"user{i}", "synthetic-password", status="active")
        with ThreadPoolExecutor(max_workers=20) as pool:
            clients = list(pool.map(lambda i: self.client(f"user{i}", create=False), range(20)))
        self.wait(lambda: all(len(c.online_users) == 20 for c in clients))
        source = Path(self.tmp.name) / "payload.bin"
        contents = bytes(range(256)) * (40 * 1024) + b"partial"
        source.write_bytes(contents)
        tid = clients[0].offer_file(source, "user1")
        self.wait(lambda: tid in clients[1].files.incoming)
        clients[1].files.accept(tid)
        for i in range(2, 20):
            clients[i].send_private("user0", f"concurrent-{i}")
        notices = []

        def chats_arrived():
            notices.extend(clients[0].drain_notices())
            return sum(n.kind == "chat" and "concurrent-" in n.text for n in notices) == 18

        self.wait(chats_arrived)
        self.wait(lambda: tid in clients[0].files.terminal, timeout=30)
        # Downloads are partitioned per receiving account: tmp/user1/user1/.
        self.assertEqual(
            (Path(self.tmp.name) / "user1" / "user1" / source.name).read_bytes(),
            contents,
        )
        notices.extend(clients[0].drain_notices())
        self.assertTrue(any("verified" in n.text.lower() for n in notices))

    def test_empty_file_and_takeover_cancel_without_stale_offline(self):
        alice, bob = self.client("alice"), self.client("bob")
        source = Path(self.tmp.name) / "empty.txt"
        source.write_bytes(b"")
        tid = alice.offer_file(source, "bob")
        self.wait(lambda: tid in bob.files.incoming)
        bob.files.accept(tid)
        self.wait(lambda: tid in alice.files.terminal)
        self.assertEqual((Path(self.tmp.name) / "bob" / "bob" / "empty.txt").read_bytes(), b"")
        tid = alice.offer_file(source, "bob")
        self.wait(lambda: tid in bob.files.incoming)
        replacement = self.client("alice", create=False)
        self.wait(lambda: alice.state == ClientState.CLOSED and tid in bob.files.terminal)
        self.assertIn("alice", bob.online_users)
        replacement.send_private("bob", "new-session")
        notices = []

        def received():
            notices.extend(bob.drain_notices())
            return any("new-session" in n.text for n in notices)

        self.wait(received)

    def test_duplicate_chat_is_not_forwarded_twice_and_semantic_error_survives(self):
        self.accounts.create_account("alice", "synthetic-password", status="active")
        bob = self.client("bob")
        alice = self.transport()
        alice.send(
            Envelope(
                "AUTH_REQUEST", "auth", {"username": "alice", "password": "synthetic-password"}
            )
        )
        self.receive_kind(alice, "AUTH_RESULT")
        message = Envelope("CHAT_PRIVATE", "same-id", {"to": "bob", "text": "only-once"})
        alice.send(message)
        self.receive_kind(alice, "CHAT_ACK")
        alice.send(message)
        self.assertEqual(self.receive_kind(alice, "ERROR").payload["code"], "INVALID_MESSAGE")
        alice.send(Envelope("PING", "still-alive", {"nonce": "alive"}))
        self.assertEqual(self.receive_kind(alice, "PONG").request_id, "still-alive")
        notices = []
        self.wait(
            lambda: (
                bool(notices.extend(bob.drain_notices()))
                or any("only-once" in n.text for n in notices)
            )
        )
        self.assertEqual(sum("only-once" in n.text for n in notices), 1)

    def test_heartbeat_and_unresponsive_tcp_peer_cleanup(self):
        with DiscoveryClient(udp_port=self.server.udp_port) as discovery:
            self.assertEqual(discovery.heartbeat("127.0.0.1").received, 1)
        self.server.ping_interval = 0.1
        self.accounts.create_account("silent", "synthetic-password", status="active")
        transport = self.transport()
        transport.send(
            Envelope(
                "AUTH_REQUEST", "auth", {"username": "silent", "password": "synthetic-password"}
            )
        )
        self.receive_kind(transport, "AUTH_RESULT")
        self.wait(lambda: self.server.active_connections == 0)

    def test_delayed_pongs_match_each_outstanding_probe(self):
        self.server.ping_interval = 0.1
        self.accounts.create_account("alice", "synthetic-password", status="active")
        transport = self.transport()
        transport.send(
            Envelope(
                "AUTH_REQUEST", "auth", {"username": "alice", "password": "synthetic-password"}
            )
        )
        self.receive_kind(transport, "AUTH_RESULT")
        probes = [self.receive_kind(transport, "PING") for _ in range(2)]
        for probe in probes:
            transport.send(Envelope("PONG", probe.request_id, probe.payload))
        transport.send(Envelope("PING", "client-ping", {"nonce": "still-alive"}))
        while True:
            response = transport.receive(timeout=2)
            self.assertNotEqual(response.type, "ERROR")
            if response.type == "PONG":
                self.assertEqual(response.request_id, "client-ping")
                break

    def test_invalid_payload_returns_correlated_error_and_consumes_id(self):
        transport = self.transport()
        body = json.dumps(
            {
                "version": 1,
                "type": "AUTH_REQUEST",
                "request_id": "bad",
                "payload": {"username": "alice", "password": 123},
            }
        ).encode()
        transport._sock.send(struct.pack("!I", len(body)) + body)
        error = self.receive_kind(transport, "ERROR")
        self.assertEqual(error.request_id, "bad")
        transport.send(
            Envelope("AUTH_REQUEST", "bad", {"username": "alice", "password": "synthetic-password"})
        )
        self.assertEqual(self.receive_kind(transport, "ERROR").payload["code"], "INVALID_MESSAGE")
        transport.send(
            Envelope(
                "AUTH_REQUEST", "fresh", {"username": "alice", "password": "synthetic-password"}
            )
        )
        self.assertEqual(self.receive_kind(transport, "AUTH_RESULT").request_id, "fresh")

    def test_partial_auth_frame_cannot_extend_absolute_deadline(self):
        self.server.first_message_timeout = 0.3
        transport = self.transport()
        transport._sock.send(b"\x00")
        started = monotonic()
        with self.assertRaises(ConnectionError):
            transport.receive(timeout=2)
        self.assertLess(monotonic() - started, 1.5)

    def stall_writer(self, username, kind):
        sid = self.handler._by_username[username]
        transport = self.server._transports[sid]
        original = transport.send
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def delayed(message):
            if message.type == kind:
                entered.set()
                release.wait(3)
            return original(message)

        transport.send = delayed
        return self.server._queues[sid], entered, release

    def test_slow_control_recipient_closes_without_blocking_sender(self):
        alice, bob = self.client("alice"), self.client("bob")
        queue, entered, release = self.stall_writer("bob", "CHAT_PRIVATE")
        queue.control_limit = 1
        alice.send_private("bob", "first")
        self.assertTrue(entered.wait(2))
        alice.send_private("bob", "second")
        alice.send_private("bob", "overflow")
        self.wait(lambda: bob.state is ClientState.CLOSED)
        self.assertIs(alice.state, ClientState.AUTHENTICATED)
        release.set()

    def test_full_file_queue_cancels_and_removes_partial_file(self):
        alice, bob = self.client("alice"), self.client("bob")
        queue, entered, release = self.stall_writer("bob", "FILE_CHUNK")
        queue.file_limit = 1
        original_send = alice.transport.send

        def wait_for_first_write(message):
            original_send(message)
            if message.type == "FILE_CHUNK" and message.payload["seq"] == 0:
                if not entered.wait(3):
                    raise TimeoutError("Receiver writer never started")

        alice.transport.send = wait_for_first_write
        source = Path(self.tmp.name) / "large.bin"
        source.write_bytes(b"x" * (16384 * 8))
        tid = alice.offer_file(source, "bob")
        self.wait(lambda: tid in bob.files.incoming)
        bob.files.accept(tid)
        self.assertTrue(entered.wait(2))
        self.wait(lambda: tid in alice.files.terminal)
        release.set()
        self.wait(lambda: tid in bob.files.terminal)
        # No partial file left after the cancel; the (empty) per-account
        # downloads folder itself may remain.
        leftovers = [path for path in (Path(self.tmp.name) / "bob").rglob("*") if path.is_file()]
        self.assertFalse(leftovers)
        self.assertIs(alice.state, ClientState.AUTHENTICATED)
        self.assertIs(bob.state, ClientState.AUTHENTICATED)

    def test_receiver_disconnect_cancels_sender(self):
        alice, bob = self.client("alice"), self.client("bob")
        source = Path(self.tmp.name) / "pending.txt"
        source.write_bytes(b"data")
        tid = alice.offer_file(source, "bob")
        self.wait(lambda: tid in bob.files.incoming)
        bob.close()
        self.wait(lambda: tid in alice.files.terminal)
        self.assertEqual(self.handler.transfers.state_of(tid), None)


if __name__ == "__main__":
    unittest.main()
