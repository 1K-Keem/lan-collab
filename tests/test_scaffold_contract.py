"""Fixture/interface checks only; no production protocol validator or networking."""

import json
import unittest
from pathlib import Path

from lancollab.common.contracts import (
    Envelope,
    MessageTransport,
    OutboundEvent,
    PROTOCOL_VERSION,
    SessionIdentity,
)
from lancollab.common.examples import example_cases, example_messages
from lancollab.common.fake_transport import FakeTransport
from lancollab.common.message_schema import FIELD_LIMITS, MESSAGE_FIELDS


def matches_fields(direction: str, message: Envelope) -> bool:
    """Test-only shape check: never substitute this for production validation."""
    spec = MESSAGE_FIELDS.get(f"{direction}:{message.type}")
    if spec is None:
        return False
    keys = set(message.payload)
    return set(spec["required"]) <= keys <= set(spec["required"]) | set(spec["optional"])


class ContractScaffoldTests(unittest.TestCase):
    def test_fixture_envelopes_match_exact_direction_schema(self) -> None:
        cases = example_cases()
        self.assertEqual(set(example_messages()) - set(cases), set())
        for name, case in cases.items():
            with self.subTest(name=name):
                message = case.message
                self.assertIsInstance(message, Envelope)
                self.assertEqual(message.version, PROTOCOL_VERSION)
                self.assertTrue(1 <= len(message.request_id) <= FIELD_LIMITS["request_id_chars"])
                self.assertTrue(all(0x21 <= ord(c) <= 0x7E for c in message.request_id))
                self.assertIsInstance(message.payload, dict)
                self.assertTrue(matches_fields(case.direction, message))
                if message.type == "PRESENCE_UPDATE":
                    fields = {"username", "status"}
                    if message.payload["status"] == "online":
                        fields |= {"ip", "port"}
                    self.assertEqual(set(message.payload), fields)
                if message.type == "AUTH_RESULT":
                    fields = {"ok", "username" if message.payload["ok"] else "error_code"}
                    self.assertEqual(set(message.payload), fields)

    def test_wrong_direction_private_message_does_not_match(self) -> None:
        cases = example_cases()
        self.assertFalse(matches_fields("C", cases["routed_private"].message))
        self.assertFalse(matches_fields("S", cases["private_chat"].message))
        self.assertFalse(matches_fields("C", cases["presence_online"].message))
        self.assertFalse(matches_fields("C", cases["auth_ok"].message))

    def test_missing_extra_and_unknown_fixture_fields_do_not_match(self) -> None:
        self.assertFalse(matches_fields("C", Envelope("CHAT_PRIVATE", "x", {"text": "hi"})))
        self.assertFalse(
            matches_fields(
                "C", Envelope("CHAT_PRIVATE", "x", {"to": "bob", "text": "hi", "from": "eve"})
            )
        )
        self.assertFalse(matches_fields("C", Envelope("UNKNOWN", "x", {})))

    def test_catalog_limits_and_removed_fields(self) -> None:
        self.assertNotIn("PRESENCE_BEACON", str(MESSAGE_FIELDS))
        self.assertNotIn("ref_request_id", str(MESSAGE_FIELDS))
        self.assertIn("C:FILE_ACK", MESSAGE_FIELDS)
        self.assertIn("S:FILE_ACK", MESSAGE_FIELDS)
        self.assertEqual(FIELD_LIMITS["max_file_window_chunks"], 8)
        self.assertEqual(FIELD_LIMITS["file_ack_interval_chunks"], 4)
        self.assertEqual(FIELD_LIMITS["max_seen_transfer_ids_per_session"], 1024)
        self.assertEqual(FIELD_LIMITS["max_request_ids_per_session"], 65536)

    def test_json_fixtures_have_direction_outside_wire_envelope(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "sample-messages.json"
        for case in json.loads(fixture_path.read_text(encoding="utf-8")):
            self.assertEqual(set(case), {"direction", "message"})
            item = case["message"]
            self.assertEqual(item["version"], PROTOCOL_VERSION)
            self.assertEqual(set(item), {"version", "type", "request_id", "payload"})
            self.assertTrue(matches_fields(case["direction"], Envelope(**item)))

    def test_events_preserve_concrete_session_target_on_takeover(self) -> None:
        old = SessionIdentity("192.0.2.1", 50001, "session-old", "alice")
        replacement = SessionIdentity("192.0.2.1", 50002, "session-new", "alice")
        event = OutboundEvent(old, example_messages()["hello_ack"])
        self.assertEqual(event.recipient, old)
        self.assertNotEqual(event.recipient, replacement)

    def test_ping_pong_fixture_echoes_id_and_nonce(self) -> None:
        cases = example_cases()
        ping, pong = cases["ping"], cases["pong"]
        self.assertEqual((ping.direction, pong.direction), ("S", "C"))
        self.assertEqual(ping.message.request_id, pong.message.request_id)
        self.assertEqual(ping.message.payload, pong.message.payload)

    def test_fake_peer_contract(self) -> None:
        client, server = FakeTransport.pair()
        self.assertIsInstance(client, MessageTransport)
        hello = example_messages()["hello"]
        client.send(hello)
        self.assertEqual(server.receive(), hello)
        with self.assertRaises(TimeoutError):
            server.receive()
        client.close()
        with self.assertRaises(BrokenPipeError):
            server.send(hello)


if __name__ == "__main__":
    unittest.main()
