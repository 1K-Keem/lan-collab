"""Offline message queue: store private chat, deliver on login.

Files are NOT queued (live-only by design) and group chat is NOT queued
(live broadcast). Only CHAT_PRIVATE to an existing, active, offline user
is stored, with CHAT_ACK {"status": "stored"}; anything else is
USER_OFFLINE as before.
"""

import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from threading import Thread

from lancollab.client.application import (
    ClientState,
    CollabClient,
    Notice,
)
from lancollab.common.contracts import Envelope, SessionIdentity
from lancollab.common.devcerts import generate_dev_certs
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.queue import MAX_QUEUED_PER_USER, MessageQueue
from tests.fake_transport import FakeTransport


def _events_by_type(events, kind):
    return [e for e in events if e.message.type == kind]


class MessageQueueTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db_path = Path(self.work.name) / "accounts.db"
        self.queue = MessageQueue(self.db_path)
        self.addCleanup(self.queue.close)

    def test_enqueue_drain_fifo_and_empty(self):
        self.queue.enqueue("bob", "alice", "CHAT_PRIVATE", {"text": "one"})
        self.queue.enqueue("bob", "alice", "CHAT_PRIVATE", {"text": "two"})
        self.queue.enqueue("bob", "carol", "CHAT_PRIVATE", {"text": "three"})
        drained = self.queue.drain("bob")
        self.assertEqual([d["payload"]["text"] for d in drained], ["one", "two", "three"])
        self.assertEqual([d["sender"] for d in drained], ["alice", "alice", "carol"])
        self.assertEqual(self.queue.drain("bob"), [])
        self.assertEqual(self.queue.count("bob"), 0)

    def test_cap_drops_oldest(self):
        for i in range(MAX_QUEUED_PER_USER + 5):
            self.queue.enqueue("bob", "alice", "CHAT_PRIVATE", {"text": f"msg-{i}"})
        self.assertEqual(self.queue.count("bob"), MAX_QUEUED_PER_USER)
        drained = self.queue.drain("bob")
        self.assertEqual(drained[0]["payload"]["text"], "msg-5")
        self.assertEqual(drained[-1]["payload"]["text"], f"msg-{MAX_QUEUED_PER_USER + 4}")

    def test_expired_messages_are_dropped(self):
        self.queue.enqueue("bob", "alice", "CHAT_PRIVATE", {"text": "old"})
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE queued_messages SET expires_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.queue.drain("bob"), [])
        self.assertEqual(self.queue.count("bob"), 0)

    def test_count_does_not_hold_the_database_lock(self):
        """count() prunes expired rows (a DELETE). If it never commits,
        its connection keeps the write lock and a second connection to the
        same file cannot drain — the live server then drops the login."""
        self.queue.enqueue("bob", "alice", "CHAT_PRIVATE", {"text": "one"})
        other = MessageQueue(self.db_path)
        self.addCleanup(other.close)
        self.assertEqual(other.count("bob"), 1)
        # A different connection must still be able to write right away.
        self.assertEqual([d["payload"]["text"] for d in self.queue.drain("bob")], ["one"])
        self.assertEqual(other.count("bob"), 0)

    def test_drop_for_user(self):
        self.queue.enqueue("bob", "alice", "CHAT_PRIVATE", {"text": "hi"})
        self.queue.enqueue("carol", "alice", "CHAT_PRIVATE", {"text": "hi"})
        self.queue.drop_for_user("bob")
        self.assertEqual(self.queue.drain("bob"), [])
        self.assertEqual(len(self.queue.drain("carol")), 1)


class ServerQueueTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db_path = Path(self.work.name) / "accounts.db"
        self.accounts = AccountService(self.db_path)
        self.addCleanup(self.accounts.close)
        self.queue = MessageQueue(self.db_path)
        self.addCleanup(self.queue.close)
        self.server = CollabServer(self.accounts, None, self.queue)
        self.accounts.create_account("admin", "admin-pass-123", role="admin", status="active")
        self.accounts.create_account("alice", "alice-pass-123", status="active")
        self.accounts.create_account("bob", "bob-pass-123", status="active")
        self.accounts.create_account("carol", "carol-pass-123")  # pending
        self._seq = 0

    def _session(self, tag):
        self._seq += 1
        return SessionIdentity("127.0.0.1", 50000 + self._seq, f"s-{tag}-{self._seq}")

    def _login(self, username, password):
        """Log in; return (identity, all events of the login)."""
        session = self._session(username)
        events = self.server.handle_message(
            Envelope(
                "AUTH_REQUEST", f"login-{username}", {"username": username, "password": password}
            ),
            session,
        )
        auth = _events_by_type(events, "AUTH_RESULT")
        self.assertEqual(len(auth), 1)
        self.assertTrue(auth[0].message.payload["ok"])
        return auth[0].recipient, events

    def _send_private(self, sender, to, text, rid):
        return self.server.handle_message(
            Envelope("CHAT_PRIVATE", rid, {"to": to, "text": text}), sender
        )

    def test_offline_active_user_gets_stored_ack_and_delivery_on_login(self):
        alice, _ = self._login("alice", "alice-pass-123")
        reply = self._send_private(alice, "bob", "hello while away", "r1")
        self.assertEqual(len(reply), 1)
        self.assertEqual(reply[0].message.type, "CHAT_ACK")
        self.assertEqual(reply[0].message.payload, {"status": "stored"})
        self.assertEqual(self.queue.count("bob"), 1)

        _, bob_events = self._login("bob", "bob-pass-123")
        queued = _events_by_type(bob_events, "CHAT_PRIVATE")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].message.payload["from"], "alice")
        self.assertEqual(queued[0].message.payload["text"], "hello while away")
        self.assertTrue(queued[0].message.payload["queued"])
        self.assertEqual(self.queue.count("bob"), 0)

    def test_second_login_gets_nothing(self):
        alice, _ = self._login("alice", "alice-pass-123")
        self._send_private(alice, "bob", "once only", "r1")
        _, first = self._login("bob", "bob-pass-123")
        self.assertEqual(len(_events_by_type(first, "CHAT_PRIVATE")), 1)
        # New session (takeover): the queue was already drained.
        _, second = self._login("bob", "bob-pass-123")
        self.assertEqual(_events_by_type(second, "CHAT_PRIVATE"), [])

    def test_unknown_pending_user_stays_offline(self):
        alice, _ = self._login("alice", "alice-pass-123")
        for to in ("ghost", "carol"):
            reply = self._send_private(alice, to, "hi", f"r-{to}")
            self.assertEqual(
                [(e.message.type, e.message.payload["code"]) for e in reply],
                [("ERROR", "USER_OFFLINE")],
            )
        self.assertEqual(self.queue.count("ghost"), 0)
        self.assertEqual(self.queue.count("carol"), 0)

    def test_online_recipient_still_live(self):
        alice, _ = self._login("alice", "alice-pass-123")
        bob, _ = self._login("bob", "bob-pass-123")
        reply = self._send_private(alice, "bob", "live", "r1")
        kinds = [e.message.type for e in reply]
        self.assertIn("CHAT_PRIVATE", kinds)
        self.assertIn("CHAT_ACK", kinds)
        acks = _events_by_type(reply, "CHAT_ACK")
        self.assertEqual(acks[0].message.payload, {"status": "queued"})
        self.assertEqual(self.queue.count("bob"), 0)

    def test_no_queue_server_keeps_old_behavior(self):
        plain = CollabServer(self.accounts, None, None)
        session = self._session("alice")
        events = plain.handle_message(
            Envelope("AUTH_REQUEST", "l1", {"username": "alice", "password": "alice-pass-123"}),
            session,
        )
        alice = _events_by_type(events, "AUTH_RESULT")[0].recipient
        reply = plain.handle_message(
            Envelope("CHAT_PRIVATE", "r1", {"to": "bob", "text": "hi"}), alice
        )
        self.assertEqual(
            [(e.message.type, e.message.payload["code"]) for e in reply],
            [("ERROR", "USER_OFFLINE")],
        )


class ClientQueueTests(unittest.TestCase):
    def setUp(self):
        self.client_end, self.server_end = FakeTransport.pair()
        self.client = CollabClient(self.client_end, auth_allowed=True)

    def _authenticate(self):
        auth_id = self.client.login("alice", "synthetic-password-only")
        self.assertEqual(self.server_end.receive().type, "AUTH_REQUEST")
        self.client.handle_event(
            Envelope("AUTH_RESULT", auth_id, {"ok": True, "username": "alice", "role": "member"})
        )
        self.assertEqual(self.client.state, ClientState.AUTHENTICATED)

    def test_stored_ack_echo(self):
        self._authenticate()
        rid = self.client.send_private("bob", "hello bob")
        self.server_end.receive()
        self.client.handle_event(Envelope("CHAT_ACK", rid, {"status": "stored"}))
        self.assertIn(
            Notice("chat", "[CHAT_PRIVATE] you -> bob: hello bob [stored — bob is offline]"),
            self.client.drain_notices(),
        )

    def test_queued_message_notice(self):
        self._authenticate()
        self.client.handle_event(
            Envelope(
                "CHAT_PRIVATE",
                "q1",
                {"from": "bob", "text": "hi back", "queued": True},
            )
        )
        self.assertIn(
            Notice("chat", "[CHAT_PRIVATE] bob: hi back [sent while you were offline]"),
            self.client.drain_notices(),
        )


class QueueIntegrationTests(unittest.TestCase):
    """Real TLS: Alice stores a message for offline Bob; Bob gets it on login."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.certs = generate_dev_certs(cls.tmp.name)
        cls.context = create_server_context(str(cls.certs.server_cert), str(cls.certs.server_key))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db_path = Path(self.work.name) / "accounts.db"
        self.accounts = AccountService(self.db_path)
        self.addCleanup(self.accounts.close)
        self.queue = MessageQueue(self.db_path)
        self.addCleanup(self.queue.close)
        self.server = CollabServer(self.accounts, None, self.queue)
        self.accounts.create_account("alice", "alice-pass-123", status="active")
        self.accounts.create_account("bob", "bob-pass-123", status="active")
        from lancollab.server.connections import ConnectionServer

        self.conn_server = ConnectionServer(
            self.context, port=0, udp_port=0, accounts=self.accounts, handler=self.server
        )
        self.thread = Thread(target=self.conn_server.serve_forever)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.conn_server.close)

    def _connect(self):
        transport = SocketTransport.connect_verified(
            "127.0.0.1", self.conn_server.port, trusted_cert=str(self.certs.server_cert)
        )
        self.addCleanup(transport.close)
        return transport

    def _recv_until(self, transport, kind, timeout=5):
        for _ in range(20):
            msg = transport.receive(timeout=timeout)
            if msg.type == kind:
                return msg
        self.fail(f"never received {kind}")

    def test_store_and_deliver_over_tls(self):
        alice = self._connect()
        alice.send(
            Envelope("AUTH_REQUEST", "a1", {"username": "alice", "password": "alice-pass-123"})
        )
        auth = self._recv_until(alice, "AUTH_RESULT")
        self.assertTrue(auth.payload["ok"])

        alice.send(Envelope("CHAT_PRIVATE", "a2", {"to": "bob", "text": "hello while away"}))
        ack = self._recv_until(alice, "CHAT_ACK")
        self.assertEqual(ack.request_id, "a2")
        self.assertEqual(ack.payload, {"status": "stored"})

        bob = self._connect()
        bob.send(Envelope("AUTH_REQUEST", "b1", {"username": "bob", "password": "bob-pass-123"}))
        auth = self._recv_until(bob, "AUTH_RESULT")
        self.assertTrue(auth.payload["ok"])
        queued = self._recv_until(bob, "CHAT_PRIVATE")
        self.assertEqual(queued.payload["from"], "alice")
        self.assertEqual(queued.payload["text"], "hello while away")
        self.assertTrue(queued.payload["queued"])
        self.assertEqual(self.queue.count("bob"), 0)


if __name__ == "__main__":
    unittest.main()
