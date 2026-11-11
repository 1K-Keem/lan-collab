import json
import unittest

from lancollab.common.udp_validation import (
    reject_duplicate_keys,
    validate_datagram_size,
    validate_udp_message,
)


class UDPValidationTests(unittest.TestCase):
    def test_all_message_types(self):
        for kind, fields in (
            ("DISCOVER", {}),
            ("DISCOVER_REPLY", {"server_name": "lan-collab.local", "tcp_port": 40000}),
            ("HEARTBEAT", {"seq": 1}),
            ("HEARTBEAT_ACK", {"seq": 1}),
        ):
            message = {"version": 1, "type": kind, "instance_id": "test", **fields}
            with self.subTest(kind=kind):
                validate_udp_message(message)
                validate_udp_message(json.loads(json.dumps(message)))

    def test_invalid_fields_and_types(self):
        valid = {
            "version": 1,
            "type": "DISCOVER_REPLY",
            "instance_id": "test",
            "server_name": "server",
            "tcp_port": 40000,
        }
        invalid = [[], None, {}, {**valid, "extra": 1}]
        for key, value in (
            ("version", True),
            ("version", 2),
            ("type", []),
            ("instance_id", 3),
            ("server_name", ""),
            ("tcp_port", True),
            ("tcp_port", 0),
            ("tcp_port", 65536),
        ):
            invalid.append({**valid, key: value})
        invalid.append({key: value for key, value in valid.items() if key != "tcp_port"})
        invalid.append({"version": 1, "type": "HEARTBEAT_ACK", "instance_id": "t", "seq": True})
        for message in invalid:
            with self.subTest(message=message), self.assertRaises(ValueError):
                validate_udp_message(message)

    def test_encoded_byte_limits(self):
        validate_datagram_size(b"x")
        validate_datagram_size(b"x" * 1200)
        for data in (b"", b"x" * 1201, ("é" * 601).encode("utf-8")):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                validate_datagram_size(data)

    def test_duplicate_keys_rejected_during_json_parsing(self):
        with self.assertRaises(ValueError):
            json.loads('{"version":1,"version":2}', object_pairs_hook=reject_duplicate_keys)
