"""Mock-only example messages matching the v1.1 declarative schema."""

from base64 import b64encode
from hashlib import sha256

from .contracts import Envelope


def example_messages() -> dict[str, Envelope]:
    """Use synthetic payloads; this is NOT a working protocol implementation."""
    data = b"hello"
    transfer_id = "b1b2b3b4-1111-4222-8333-444455556666"
    return {
        "hello": Envelope("HELLO", "req-hello", {"client_name": "mock-client"}),
        "hello_ack": Envelope(
            "HELLO_ACK", "req-hello", {"server_name": "mock-server", "protocol_version": 1}
        ),
        "auth": Envelope(
            "AUTH_REQUEST",
            "req-auth",
            {"username": "alice", "password": "mock-only-never-use"},
        ),
        "auth_ok": Envelope("AUTH_RESULT", "req-auth", {"ok": True, "username": "alice"}),
        "user_list": Envelope(
            "USER_LIST",
            "event-1",
            {"users": [{"username": "bob", "ip": "192.0.2.2", "port": 50123}]},
        ),
        "presence_online": Envelope(
            "PRESENCE_UPDATE",
            "event-2",
            {"username": "bob", "status": "online", "ip": "192.0.2.2", "port": 50123},
        ),
        "presence_offline": Envelope(
            "PRESENCE_UPDATE", "event-3", {"username": "bob", "status": "offline"}
        ),
        "global_chat": Envelope("CHAT_GLOBAL", "req-global", {"text": "Hello all"}),
        "private_chat": Envelope(
            "CHAT_PRIVATE", "req-private", {"to": "bob", "text": "Hello Bob"}
        ),
        "chat_ack": Envelope("CHAT_ACK", "req-private", {"status": "queued"}),
        "file_offer": Envelope(
            "FILE_OFFER",
            "req-offer",
            {
                "transfer_id": transfer_id,
                "to": "bob",
                "name": "example.txt",
                "size": len(data),
                "sha256": sha256(data).hexdigest(),
            },
        ),
        "file_accept": Envelope("FILE_ACCEPT", "req-accept", {"transfer_id": transfer_id}),
        "file_chunk": Envelope(
            "FILE_CHUNK",
            "req-chunk-0",
            {
                "transfer_id": transfer_id,
                "seq": 0,
                "offset": 0,
                "data_b64": b64encode(data).decode("ascii"),
            },
        ),
        "file_ack": Envelope(
            "FILE_ACK", "req-ack-0", {"transfer_id": transfer_id, "next_offset": len(data)}
        ),
        "file_end": Envelope(
            "FILE_END", "req-end", {"transfer_id": transfer_id, "total_chunks": 1}
        ),
        "file_result": Envelope(
            "FILE_RESULT", "req-result", {"transfer_id": transfer_id, "ok": True}
        ),
        "file_cancel": Envelope(
            "FILE_CANCEL", "req-cancel", {"transfer_id": transfer_id, "reason": "TIMEOUT"}
        ),
        "ping": Envelope("PING", "req-ping", {"nonce": "dummy-nonce"}),
        "pong": Envelope("PONG", "req-ping", {"nonce": "dummy-nonce"}),
        "error": Envelope(
            "ERROR",
            "req-private",
            {"code": "USER_OFFLINE", "message": "Recipient unavailable"},
        ),
    }
