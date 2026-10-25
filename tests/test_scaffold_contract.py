"""Smoke checks for contract drift ONLY, not production network behavior."""

import json
import unittest
from pathlib import Path

from lancollab.common.contracts import Envelope, MessageTransport, PROTOCOL_VERSION
from lancollab.common.examples import example_messages
from lancollab.common.fake_transport import FakeTransport
from lancollab.common.message_schema import FIELD_LIMITS, MESSAGE_FIELDS


class ContractScaffoldTests(unittest.TestCase):
    def test_fixture_envelopes_match_known_schema_field_names(self) -> None:
        messages = example_messages()
        self.assertGreaterEqual(len(messages), 18)
        for message in messages.values():
            self.assertIsInstance(message, Envelope)
            self.assertEqual(message.version, PROTOCOL_VERSION)
            self.assertTrue(message.request_id)
            self.assertIsInstance(message.payload, dict)

            candidates = [
                layout for key, layout in MESSAGE_FIELDS.items() if key.endswith(":" + message.type)
            ]
            self.assertTrue(candidates, f"Missing schema for {message.type}")
            keys = set(message.payload)
            self.assertTrue(
                any(
                    set(spec["required"]) <= keys
                    and keys <= set(spec["required"]) | set(spec["optional"])
                    for spec in candidates
                ),
                f"{message.type} has incorrect required/extra keys",
            )

    def test_v11_removed_ambiguous_fields(self) -> None:
        self.assertNotIn("PRESENCE_BEACON", str(MESSAGE_FIELDS))
        self.assertNotIn("ref_request_id", str(MESSAGE_FIELDS))
        self.assertIn("C:FILE_ACK", MESSAGE_FIELDS)
        self.assertIn("S:FILE_ACK", MESSAGE_FIELDS)
        self.assertEqual(FIELD_LIMITS["max_file_window_chunks"], 8)
        self.assertEqual(FIELD_LIMITS["file_ack_interval_chunks"], 4)

    def test_json_fixtures_have_message_envelope(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "sample-messages.json"
        for item in json.loads(fixture_path.read_text(encoding="utf-8")):
            self.assertEqual(item["version"], PROTOCOL_VERSION)
            self.assertEqual(set(item), {"version", "type", "request_id", "payload"})

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

    def test_real_codec_stays_todo(self) -> None:
        from lancollab.common.socket_transport import JsonFrameCodec

        with self.assertRaises(NotImplementedError):
            JsonFrameCodec().encode(example_messages()["hello"])


if __name__ == "__main__":
    unittest.main()
