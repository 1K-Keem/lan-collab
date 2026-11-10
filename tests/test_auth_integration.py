"""End-to-end auth over real TLS: admin, register, approve, login, reject."""

import tempfile
import unittest
from pathlib import Path
from threading import Thread

from lancollab.common.devcerts import generate_dev_certs
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context
from lancollab.common.contracts import Envelope
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.connections import ConnectionServer


class AuthIntegrationTests(unittest.TestCase):
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
        self.accounts = AccountService(Path(self.work.name) / "accounts.db")
        self.addCleanup(self.accounts.close)
        self.accounts.create_account("admin", "admin-pass-123", role="admin", status="active")
        self.server = ConnectionServer(
            self.context,
            port=0,
            udp_port=0,
            accounts=self.accounts,
            handler=CollabServer(self.accounts),
        )
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.close)

    def connect(self):
        t = SocketTransport.connect_verified(
            "127.0.0.1", self.server.port, trusted_cert=str(self.certs.server_cert)
        )
        self.addCleanup(t.close)
        return t

    def request(self, transport, kind, payload, rid="r1"):
        transport.send(Envelope(kind, rid, payload))
        for _ in range(10):
            reply = transport.receive(timeout=5)
            if reply.request_id == rid:
                return reply
            # Otherwise it's an async message (USER_LIST / PRESENCE_UPDATE):
            # skip it and keep reading for our reply.
        raise AssertionError(f"No reply with request_id {rid!r}")

    def test_register_approve_login(self):
        alice = self.connect()
        reply = self.request(
            alice, "REGISTER_REQUEST", {"username": "alice", "password": "password123"}
        )
        self.assertEqual(reply.payload, {"ok": True, "status": "pending"})
        # Login while pending fails.
        alice2 = self.connect()
        reply = self.request(
            alice2, "AUTH_REQUEST", {"username": "alice", "password": "password123"}
        )
        self.assertEqual(reply.payload, {"ok": False, "error_code": "ACCOUNT_PENDING"})
        # Admin approves, then login succeeds with role.
        admin = self.connect()
        reply = self.request(
            admin, "AUTH_REQUEST", {"username": "admin", "password": "admin-pass-123"}
        )
        self.assertEqual(reply.payload, {"ok": True, "username": "admin", "role": "admin"})
        reply = self.request(admin, "PENDING_USERS_REQUEST", {}, rid="r2")
        self.assertEqual([u["username"] for u in reply.payload["users"]], ["alice"])
        reply = self.request(
            admin, "ACCOUNT_REVIEW_REQUEST", {"username": "alice", "decision": "approve"}, rid="r3"
        )
        self.assertEqual(reply.payload, {"ok": True, "username": "alice", "status": "active"})
        alice3 = self.connect()
        reply = self.request(
            alice3, "AUTH_REQUEST", {"username": "alice", "password": "password123"}
        )
        self.assertEqual(reply.payload, {"ok": True, "username": "alice", "role": "member"})

    def test_member_cannot_review(self):
        self.accounts.create_account("alice", "password123", status="active")
        alice = self.connect()
        self.request(alice, "AUTH_REQUEST", {"username": "alice", "password": "password123"})
        reply = self.request(alice, "PENDING_USERS_REQUEST", {}, rid="r2")
        self.assertEqual(reply.type, "ERROR")
        self.assertEqual(reply.payload["code"], "NOT_AUTHORIZED")
