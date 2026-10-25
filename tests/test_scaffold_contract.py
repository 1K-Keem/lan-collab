"""Smoke tests of dummy interfaces ONLY, not project acceptance tests."""

import unittest

from lancollab.common.contracts import Envelope, MessageTransport, PROTOCOL_VERSION
from lancollab.common.examples import example_messages
from lancollab.common.fake_transport import FakeTransport


class ContractScaffoldTests(unittest.TestCase):
    def test_fixture_envelopes_have_expected_shape(self) -> None:
        messages = example_messages()
        self.assertGreaterEqual(len(messages), 10)
        for message in messages.values():
            self.assertIsInstance(message, Envelope)
            self.assertEqual(message.version, PROTOCOL_VERSION)
            self.assertTrue(message.type)
            self.assertTrue(message.request_id)
            self.assertIsInstance(message.payload, dict)

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
