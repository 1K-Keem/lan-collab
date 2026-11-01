import unittest
from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import JsonFrameCodec


class TestJsonFrameCodec(unittest.TestCase):
    def setUp(self):
        self.codec = JsonFrameCodec()
        self.valid_message = Envelope(
            version=1, type="HELLO", request_id="req-123", payload={"client_name": "test-client"}
        )

    def test_encode_decode_success(self):
        # Test that encoding and then decoding returns the exact same data
        encoded_bytes = self.codec.encode(self.valid_message)

        # Skip the 4-byte big-endian length header
        json_payload_bytes = encoded_bytes[4:]

        decoded_message = self.codec.decode(json_payload_bytes)

        self.assertEqual(decoded_message.type, "HELLO")
        self.assertEqual(decoded_message.request_id, "req-123")
        self.assertEqual(decoded_message.payload["client_name"], "test-client")

    def test_payload_too_large_raises_error(self):
        # Create a message with a payload that is too large (over 256 KiB)
        huge_message = Envelope(
            version=1, type="CHAT_GLOBAL", request_id="req-huge", payload={"text": "A" * 300000}
        )
        with self.assertRaises(ValueError):
            self.codec.encode(huge_message)
