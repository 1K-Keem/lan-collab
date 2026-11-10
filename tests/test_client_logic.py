"""Example mock tests for Issue #7; not real TCP/TLS acceptance tests."""

import unittest

from lancollab.client.application import (
    ClientState,
    CollabClient,
    ProtocolViolation,
    safe_display,
)
from lancollab.common.contracts import Envelope
from lancollab.common.fake_transport import FakeTransport


class ClientLogicTests(unittest.TestCase):
    def setUp(self):
        self.client_end, self.server_end = FakeTransport.pair()
        # Synthetic password only: FakeTransport is not encrypted.
        self.client = CollabClient(self.client_end, auth_allowed=True)

    def authenticate(self):
        auth_id = self.client.login("alice", "synthetic-password-only")
        self.assertEqual(self.server_end.receive().type, "AUTH_REQUEST")
        self.client.handle_event(
            Envelope(
                "AUTH_RESULT",
                auth_id,
                {"ok": True, "username": "alice", "role": "member"},
            )
        )
        self.assertEqual(self.client.state, ClientState.AUTHENTICATED)

    def test_direct_auth_state(self):
        self.assertEqual(self.client.state, ClientState.WAIT_AUTH)
        with self.assertRaises(RuntimeError):
            self.client.send_global("too early")
        self.authenticate()
        self.assertEqual(self.client.username, "alice")
        with self.assertRaises(RuntimeError):
            self.client.login("alice", "synthetic-password-only")

    def test_wrong_auth_result_id_closes_client(self):
        self.client.login("alice", "synthetic-password-only")
        self.server_end.receive()
        with self.assertRaises(ProtocolViolation):
            self.client.handle_event(
                Envelope(
                    "AUTH_RESULT", "wrong-id", {"ok": True, "username": "alice", "role": "member"}
                )
            )
        self.assertEqual(self.client.state, ClientState.CLOSED)

    def test_pending_auth_cannot_login_or_chat(self):
        self.client.login("alice", "synthetic-password-only")
        self.assertEqual(self.server_end.receive().type, "AUTH_REQUEST")
        with self.assertRaises(RuntimeError):
            self.client.login("bob", "synthetic-password-only")
        with self.assertRaises(RuntimeError):
            self.client.send_global("too early")

    def test_logout_before_auth(self):
        self.client.logout()
        self.assertEqual(self.server_end.receive().type, "LOGOUT")
        self.assertEqual(self.client.state, ClientState.CLOSED)

    def test_presence_before_auth_is_rejected(self):
        with self.assertRaises(ProtocolViolation):
            self.client.handle_event(
                Envelope(
                    "PRESENCE_UPDATE",
                    "event-1",
                    {"username": "bob", "status": "online", "ip": "192.0.2.1", "port": 50001},
                )
            )
        self.assertEqual(self.client.state, ClientState.CLOSED)

    def test_auth_failure_can_retry(self):
        auth_id = self.client.login("alice", "synthetic-password-only")
        self.server_end.receive()
        self.client.handle_event(
            Envelope(
                "AUTH_RESULT",
                auth_id,
                {"ok": False, "error_code": "AUTH_FAILED"},
            )
        )
        self.assertEqual(self.client.state, ClientState.WAIT_AUTH)

    def test_chat_payload_and_ack(self):
        self.authenticate()
        global_id = self.client.send_global("hello all")
        private_id = self.client.send_private("bob", "hello bob")
        msg1 = self.server_end.receive()
        msg2 = self.server_end.receive()
        self.assertEqual(msg1.type, "CHAT_GLOBAL")
        self.assertEqual(msg1.payload, {"text": "hello all"})
        self.assertEqual(msg2.payload, {"to": "bob", "text": "hello bob"})
        self.assertNotEqual(global_id, private_id)
        self.client.handle_event(Envelope("CHAT_ACK", private_id, {"status": "queued"}))
        self.assertIn("not delivered/read", self.client.drain_notices()[-1].text)

    def test_user_list_and_presence(self):
        self.authenticate()
        self.client.handle_event(
            Envelope(
                "USER_LIST",
                "server-event-1",
                {"users": [{"username": "bob", "ip": "192.0.2.5", "port": 50000}]},
            )
        )
        self.assertEqual(self.client.online_users["bob"], ("192.0.2.5", 50000))
        self.client.handle_event(
            Envelope(
                "PRESENCE_UPDATE",
                "server-event-2",
                {"username": "bob", "status": "offline"},
            )
        )
        self.assertNotIn("bob", self.client.online_users)

    def test_server_ping_is_answered_with_matching_id_and_nonce(self):
        self.authenticate()
        self.client.handle_event(Envelope("PING", "server-ping-1", {"nonce": "random"}))
        pong = self.server_end.receive()
        self.assertEqual(
            (pong.type, pong.request_id, pong.payload),
            ("PONG", "server-ping-1", {"nonce": "random"}),
        )

    def test_requires_auth_security_explicitly(self):
        insecure_end, _ = FakeTransport.pair()
        insecure_client = CollabClient(insecure_end)
        with self.assertRaises(RuntimeError):
            insecure_client.login("alice", "not-real")

    def test_control_sequences_are_escaped(self):
        self.assertEqual(safe_display("bob\x1b[31m"), "bob\\u001b[31m")
        self.assertEqual(safe_display("hi\nthere"), "hi\\u000athere")

    def test_auth_success_username_must_match_request(self):
        auth_id = self.client.login("alice", "synthetic-password-only")
        self.server_end.receive()
        with self.assertRaisesRegex(ProtocolViolation, "username/role mismatch"):
            self.client.handle_event(
                Envelope(
                    "AUTH_RESULT",
                    auth_id,
                    {"ok": True, "username": "mallory", "role": "member"},
                )
            )
        self.assertEqual(self.client.state, ClientState.CLOSED)
        self.assertIsNone(self.client.username)
        self.assertIsNone(self.client._requested_username)

    def test_auth_rejects_wrong_boolean_type_and_extra_fields(self):
        for payload in (
            {"ok": 1, "username": "alice"},
            {"ok": True, "username": "alice", "error_code": "AUTH_FAILED"},
            {"ok": False, "username": "alice", "error_code": "AUTH_FAILED"},
            {"ok": False, "error_code": "OTHER"},
        ):
            with self.subTest(payload=payload):
                client_end, server_end = FakeTransport.pair()
                client = CollabClient(client_end, auth_allowed=True)
                auth_id = client.login("alice", "synthetic-password-only")
                server_end.receive()
                with self.assertRaises(ProtocolViolation):
                    client.handle_event(Envelope("AUTH_RESULT", auth_id, payload))
                self.assertEqual(client.state, ClientState.CLOSED)

    def test_auth_fail_drops_requested_identity(self):
        req = self.client.login("alice", "synthetic-password-only")
        self.server_end.receive()
        self.client.handle_event(
            Envelope(
                "AUTH_RESULT",
                req,
                {"ok": False, "error_code": "AUTH_FAILED"},
            )
        )
        self.assertIsNone(self.client._requested_username)
        req2 = self.client.login("bob", "synthetic-password-only")
        self.server_end.receive()
        self.client.handle_event(
            Envelope(
                "AUTH_RESULT",
                req2,
                {"ok": True, "username": "bob", "role": "member"},
            )
        )
        self.assertEqual(self.client.username, "bob")

    def test_auth_error_drops_requested_identity(self):
        req = self.client.login("alice", "synthetic-password-only")
        self.server_end.receive()
        self.client.handle_event(
            Envelope(
                "ERROR",
                req,
                {"code": "INVALID_STATE", "message": "retry"},
            )
        )
        self.assertIsNone(self.client._requested_username)
        self.assertEqual(self.client.state, ClientState.WAIT_AUTH)

    def test_chat_ack_unmatched_is_rejected(self):
        self.authenticate()
        with self.assertRaises(ProtocolViolation):
            self.client.handle_event(
                Envelope(
                    "CHAT_ACK",
                    "not-a-request",
                    {"status": "queued"},
                )
            )
        self.assertEqual(self.client.state, ClientState.CLOSED)

    def test_presence_invalid_online_port_is_rejected(self):
        self.authenticate()
        with self.assertRaises(ProtocolViolation):
            self.client.handle_event(
                Envelope(
                    "PRESENCE_UPDATE",
                    "server-event-x",
                    {"username": "bob", "status": "online", "ip": "192.0.2.1", "port": True},
                )
            )
        self.assertEqual(self.client.state, ClientState.CLOSED)

    def test_presence_offline_cannot_include_ip(self):
        self.authenticate()
        with self.assertRaises(ProtocolViolation):
            self.client.handle_event(
                Envelope(
                    "PRESENCE_UPDATE",
                    "server-event-x",
                    {"username": "bob", "status": "offline", "ip": "192.0.2.1"},
                )
            )

    def test_event_version_must_be_int_one(self):
        req = self.client.login("alice", "synthetic-password-only")
        self.server_end.receive()
        with self.assertRaises(ProtocolViolation):
            self.client.handle_event(
                Envelope(
                    "AUTH_RESULT",
                    req,
                    {"ok": True, "username": "alice", "role": "member"},
                    version=True,
                )
            )
        self.assertEqual(self.client.state, ClientState.CLOSED)

    def test_close_idempotent_and_clears_pending(self):
        self.client.login("alice", "synthetic-password-only")
        self.client.close()
        self.client.close()
        self.assertEqual(self.client.state, ClientState.CLOSED)
        self.assertFalse(self.client._pending)
        self.assertIsNone(self.client._requested_username)

    def test_receive_loop_disconnect_closes_client(self):
        from threading import Event

        class EOFTransport:
            def send(self, message):
                pass

            def receive(self, *, timeout=None):
                raise ConnectionError("peer disconnected")

            def close(self):
                pass

        client = CollabClient(EOFTransport())
        client.receive_loop(Event())
        self.assertEqual(client.state, ClientState.CLOSED)
        self.assertTrue(any(n.kind == "disconnected" for n in client.drain_notices()))

    def test_unicode_invisible_control_display(self):
        self.assertEqual(safe_display("name\u200dtest"), "name\\u200dtest")

    def test_issued_chat_request_ids_unique(self):
        self.authenticate()
        first = self.client.send_global("a")
        second = self.client.send_global("b")
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(second), 64)
        self.assertEqual(len(self.client._pending), 2)

    def test_logout_emits_message_and_closes(self):
        self.authenticate()
        self.client.logout()
        message = self.server_end.receive()
        self.assertEqual(message.type, "LOGOUT")
        self.assertEqual(message.payload, {})
        self.assertEqual(self.client.state, ClientState.CLOSED)


if __name__ == "__main__":
    unittest.main()
