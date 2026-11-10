"""Real codec tests: framing, strict validation, direction and conditional rules."""

import base64
import json
import struct
import unittest

from lancollab.common.codec import (
    FrameError,
    JsonFrameCodec,
    MessageError,
    OutboundMessageError,
    RequestIdTracker,
    check_frame_length,
)
from lancollab.common.contracts import Envelope
from lancollab.common.examples import example_cases
from lancollab.common.message_schema import MESSAGE_FIELDS

SERVER = JsonFrameCodec("server")  # decodes C:, encodes S:
CLIENT = JsonFrameCodec("client")  # decodes S:, encodes C:
TID = "b1b2b3b4-1111-4222-8333-444455556666"
SHA = "a" * 64


def frame(obj: object) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def env(msg_type: str, payload: dict, request_id: str = "r1", **extra: object) -> dict:
    return {"version": 1, "type": msg_type, "request_id": request_id, "payload": payload, **extra}


class RoundTripTests(unittest.TestCase):
    def test_every_fixture_round_trips_in_its_direction(self) -> None:
        for name, case in example_cases().items():
            with self.subTest(name=name):
                sender, receiver = (CLIENT, SERVER) if case.direction == "C" else (SERVER, CLIENT)
                wire = sender.encode(case.message)
                self.assertEqual(struct.unpack(">I", wire[:4])[0], len(wire) - 4)
                self.assertEqual(receiver.decode(wire), case.message)

    def test_wrong_direction_is_rejected_both_ways(self) -> None:
        cases = example_cases()
        with self.assertRaises(OutboundMessageError):
            SERVER.encode(cases["private_chat"].message)  # C: message, server cannot send it
        with self.assertRaises(MessageError):
            CLIENT.decode(frame(env("CHAT_PRIVATE", {"to": "bob", "text": "x"})))
        # A client must never be able to originate a routed event with a forged sender.
        with self.assertRaises(MessageError):
            SERVER.decode(frame(env("CHAT_GLOBAL", {"from": "eve", "text": "hi"})))

    def test_unicode_survives_and_stays_utf8(self) -> None:
        msg = Envelope("CHAT_GLOBAL", "r1", {"text": "Xin chào 🌏"})
        wire = CLIENT.encode(msg)
        self.assertIn("Xin chào 🌏".encode(), wire)
        self.assertEqual(SERVER.decode(wire), msg)

    def test_every_catalog_entry_is_implemented(self) -> None:
        # Import-time compilation fails for unknown specs; this guards catalog drift.
        self.assertEqual(len(MESSAGE_FIELDS), 37)


class FramingTests(unittest.TestCase):
    def test_length_limits_checked_before_body(self) -> None:
        for bad in (0, 262145, 0xFFFFFFFF):
            with self.subTest(length=bad), self.assertRaises(FrameError):
                check_frame_length(bad)
        check_frame_length(1)
        check_frame_length(262144)

    def test_prefix_must_match_body(self) -> None:
        good = CLIENT.encode(Envelope("PING", "r1", {"nonce": "n"}))
        with self.assertRaises(FrameError):
            SERVER.decode(good + b"x")
        with self.assertRaises(FrameError):
            SERVER.decode(good[:-1])
        with self.assertRaises(FrameError):
            SERVER.decode(b"\x00\x00")

    def test_encoder_refuses_oversize_frame(self) -> None:
        big = Envelope("CHAT_GLOBAL", "r1", {"text": "x" * 4097})
        with self.assertRaises(OutboundMessageError):
            CLIENT.encode(big)


class EnvelopeStrictnessTests(unittest.TestCase):
    def decode(self, obj: object) -> Envelope:
        return SERVER.decode(frame(obj))

    def test_malformed_json_and_untrusted_ids_close_connection(self) -> None:
        for body in (b"not json", b"[]", b'"x"', b"\xff\xfe", b'{"version":1}', b"\xef\xbb\xbf{}"):
            with self.subTest(body=body), self.assertRaises(FrameError) as ctx:
                SERVER.decode(struct.pack(">I", len(body)) + body)
            self.assertIsNone(ctx.exception.to_error_envelope())
        for rid in ("", "a b", "caf\u00e9"):
            with self.subTest(request_id=rid), self.assertRaises(FrameError):
                self.decode(
                    env("AUTH_REQUEST", {"username": "alice", "password": "test"}, request_id=rid)
                )

    def test_semantic_errors_get_error_reply_with_same_request_id(self) -> None:
        with self.assertRaises(MessageError) as ctx:
            self.decode(env("AUTH_REQUEST", {}, request_id="abc"))
        reply = ctx.exception.to_error_envelope()
        assert reply is not None
        self.assertEqual((reply.type, reply.request_id), ("ERROR", "abc"))
        self.assertEqual(reply.payload["code"], "INVALID_MESSAGE")
        SERVER.encode_body(reply)  # the ERROR reply itself is schema-valid

    def test_duplicate_keys_extra_keys_and_non_finite_numbers(self) -> None:
        dup = b'{"version":1,"type":"AUTH_REQUEST","request_id":"r1","payload":{"username":"alice","username":"bob","password":"test"}}'
        with self.assertRaises(MessageError):
            SERVER.decode(struct.pack(">I", len(dup)) + dup)
        with self.assertRaises(MessageError):
            self.decode(env("AUTH_REQUEST", {"username": "alice", "password": "test"}, extra=1))
        with self.assertRaises(MessageError):
            self.decode(env("AUTH_REQUEST", {"username": "alice", "password": "test", "x": 1}))
        nan = b'{"version":1,"type":"PING","request_id":"r1","payload":{"nonce":NaN}}'
        with self.assertRaises(FrameError):
            SERVER.decode(struct.pack(">I", len(nan)) + nan)

    def test_version_rules(self) -> None:
        for version, code in ((2, "UNSUPPORTED_VERSION"), (0, "UNSUPPORTED_VERSION")):
            with self.subTest(version=version), self.assertRaises(MessageError) as ctx:
                obj = env("AUTH_REQUEST", {"username": "alice", "password": "test"})
                obj["version"] = version
                self.decode(obj)
            self.assertEqual(ctx.exception.code, code)
        for version in (True, 1.0, "1", None):
            with self.subTest(version=version), self.assertRaises(MessageError):
                obj = env("AUTH_REQUEST", {"username": "alice", "password": "test"})
                obj["version"] = version
                self.decode(obj)

    def test_bool_is_not_int_and_int_is_not_bool(self) -> None:
        with self.assertRaises(MessageError):
            self.decode(env("FILE_ACK", {"transfer_id": TID, "next_offset": True}))
        with self.assertRaises(MessageError):
            self.decode(env("FILE_ACK", {"transfer_id": TID, "next_offset": 1.0}))
        with self.assertRaises(MessageError):
            self.decode(env("FILE_RESULT", {"transfer_id": TID, "ok": 1}))
        self.decode(env("FILE_RESULT", {"transfer_id": TID, "ok": True}))

    def test_unknown_and_server_only_types(self) -> None:
        for msg_type in ("NOPE", "AUTH_RESULT", "USER_LIST", "PRESENCE_BEACON"):
            with self.subTest(msg_type=msg_type), self.assertRaises(MessageError):
                self.decode(env(msg_type, {}))

    def test_deeply_nested_input_cannot_crash(self) -> None:
        body = (
            b'{"version":1,"type":"AUTH_REQUEST","request_id":"r1","payload":'
            + b"[" * 5000
            + b"]" * 5000
        )
        with self.assertRaises((FrameError, MessageError)):
            SERVER.decode(struct.pack(">I", len(body + b"}")) + body + b"}")


class FieldValidationTests(unittest.TestCase):
    def bad(self, msg_type: str, payload: dict, codec: JsonFrameCodec = SERVER) -> None:
        with self.assertRaises(MessageError, msg=str(payload)):
            codec.decode(frame(env(msg_type, payload)))

    def good(self, msg_type: str, payload: dict, codec: JsonFrameCodec = SERVER) -> None:
        codec.decode(frame(env(msg_type, payload)))

    def test_username(self) -> None:
        self.good("AUTH_REQUEST", {"username": "Al.ice_-9", "password": "p"})
        for name in ("", "a" * 33, "al ice", "al\n", "ali/ce", "alicé", "x\n"):
            self.bad("AUTH_REQUEST", {"username": name, "password": "p"})

    def test_chat_text_limits_are_utf8_bytes(self) -> None:
        self.good("CHAT_GLOBAL", {"text": "a" * 4096})
        self.bad("CHAT_GLOBAL", {"text": "a" * 4097})
        self.bad("CHAT_GLOBAL", {"text": "é" * 2049})  # 4098 bytes, 2049 chars
        self.bad("CHAT_GLOBAL", {"text": ""})
        self.bad("CHAT_GLOBAL", {"text": "\ud800"})  # lone surrogate

    def test_file_offer_fields(self) -> None:
        ok = {"transfer_id": TID, "to": "bob", "name": "a.txt", "size": 5, "sha256": SHA}
        self.good("FILE_OFFER", ok)
        self.good("FILE_OFFER", {**ok, "size": 0})
        self.good("FILE_OFFER", {**ok, "size": 50 * 1024 * 1024 + 1})  # core answers TOO_LARGE
        for patch in (
            {"transfer_id": TID.upper()},
            {"transfer_id": "not-a-uuid"},
            {"transfer_id": TID.replace("-", "")},
            {"sha256": "A" * 64},
            {"sha256": "a" * 63},
            {"name": ""},
            {"name": "../x"},
            {"name": "a\\b"},
            {"name": "a\x00b"},
            {"name": "x" * 256},
            {"name": ".."},
            {"size": -1},
            {"size": 1.5},
            {"size": "5"},
        ):
            with self.subTest(patch=patch):
                self.bad("FILE_OFFER", {**ok, **patch})

    def test_chunk_payload(self) -> None:
        def chunk(data_b64: str, seq: int = 0, offset: int = 0) -> dict:
            return {"transfer_id": TID, "seq": seq, "offset": offset, "data_b64": data_b64}

        self.good("FILE_CHUNK", chunk(base64.b64encode(b"x").decode()))
        self.good("FILE_CHUNK", chunk(base64.b64encode(b"x" * 16384).decode()))
        self.bad("FILE_CHUNK", chunk(base64.b64encode(b"x" * 16385).decode()))
        self.bad("FILE_CHUNK", chunk(""))
        self.bad("FILE_CHUNK", chunk("!!!!"))
        self.bad("FILE_CHUNK", chunk("QQ"))  # unpadded
        self.bad("FILE_CHUNK", chunk("QR=="))  # non-canonical trailing bits
        self.bad("FILE_CHUNK", chunk("QQ==\n"))
        self.bad("FILE_CHUNK", chunk("QQ==", seq=-1))
        self.bad("FILE_CHUNK", chunk("QQ==", offset=-1))

    def test_conditional_auth_result(self) -> None:
        self.good("AUTH_RESULT", {"ok": True, "username": "alice", "role": "member"}, CLIENT)
        self.good("AUTH_RESULT", {"ok": False, "error_code": "AUTH_FAILED"}, CLIENT)
        self.good("AUTH_RESULT", {"ok": False, "error_code": "ACCOUNT_PENDING"}, CLIENT)
        self.good("AUTH_RESULT", {"ok": False, "error_code": "ACCOUNT_REJECTED"}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": True}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": True, "username": "a", "error_code": "AUTH_FAILED"}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": False}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": False, "error_code": "AUTH_FAILED", "username": "a"}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": False, "error_code": "OTHER"}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": True, "username": "alice"}, CLIENT)
        self.bad("AUTH_RESULT", {"ok": True, "username": "alice", "role": "superuser"}, CLIENT)
        self.bad(
            "AUTH_RESULT", {"ok": False, "error_code": "AUTH_FAILED", "role": "member"}, CLIENT
        )

    def test_conditional_presence_and_user_list(self) -> None:
        online = {"username": "bob", "status": "online", "ip": "192.0.2.2", "port": 50123}
        self.good("PRESENCE_UPDATE", online, CLIENT)
        self.good("PRESENCE_UPDATE", {**online, "ip": "2001:db8::1"}, CLIENT)
        self.good("PRESENCE_UPDATE", {"username": "bob", "status": "offline"}, CLIENT)
        self.bad("PRESENCE_UPDATE", {"username": "bob", "status": "online"}, CLIENT)
        self.bad("PRESENCE_UPDATE", {**online, "status": "offline"}, CLIENT)
        self.bad("PRESENCE_UPDATE", {**online, "ip": "999.1.1.1"}, CLIENT)
        self.bad("PRESENCE_UPDATE", {**online, "port": 0}, CLIENT)
        self.bad("PRESENCE_UPDATE", {**online, "status": "away"}, CLIENT)
        user = {"username": "bob", "ip": "192.0.2.2", "port": 5}
        self.good("USER_LIST", {"users": [user]}, CLIENT)
        self.good("USER_LIST", {"users": []}, CLIENT)
        self.bad("USER_LIST", {"users": [user, user]}, CLIENT)
        self.bad("USER_LIST", {"users": [{"username": "bob"}]}, CLIENT)
        self.bad("USER_LIST", {"users": [{**user, "x": 1}]}, CLIENT)
        self.bad("USER_LIST", {"users": "bob"}, CLIENT)

    def test_ping_nonce_and_error_codes(self) -> None:
        self.good("PONG", {"nonce": "abc123"})
        self.bad("PONG", {"nonce": ""})
        self.bad("PONG", {"nonce": "has space"})
        self.good("ERROR", {"code": "USER_OFFLINE", "message": "gone"}, CLIENT)
        self.bad("ERROR", {"code": "lowercase", "message": "gone"}, CLIENT)


class RequestIdTrackerTests(unittest.TestCase):
    def test_duplicates_rejected_and_history_bounded(self) -> None:
        tracker = RequestIdTracker(limit=3)
        for rid in ("a", "b", "c"):
            tracker.register(rid)
        with self.assertRaises(MessageError):
            tracker.register("a")
        with self.assertRaises(FrameError):  # exhausted: never evicts, session must close
            tracker.register("d")
        self.assertEqual(len(tracker), 3)


if __name__ == "__main__":
    unittest.main()
