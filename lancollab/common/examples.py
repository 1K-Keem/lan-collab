"""Synthetic message examples for contract smoke tests, not production data."""

from hashlib import sha256

from .contracts import Envelope


def example_messages() -> dict[str, Envelope]:
    """Representatives of the proposed v1 schema for mock-only development."""
    data = b"hello"
    transfer_id = "b1b2b3b4-1111-4222-8333-444455556666"

    return {
        "hello": Envelope("HELLO", "req-hello", {"client_name": "mock-client"}),
        "hello_ack": Envelope(
            "HELLO_ACK",
            "req-hello",
            {"server_name": "mock-server", "protocol_version": 1},
        ),
        "auth": Envelope(
            "AUTH_REQUEST",
            "req-auth",
            {"username": "alice", "password": "mock-only-never-use"},
        ),
        "auth_ok": Envelope("AUTH_RESULT", "req-auth", {"ok": True, "username": "alice"}),
        "global_chat": Envelope("CHAT_GLOBAL", "req-global", {"text": "Hello all"}),
        "private_chat": Envelope("CHAT_PRIVATE", "req-private", {"to": "bob", "text": "Hello Bob"}),
        "chat_ack": Envelope(
            "CHAT_ACK",
            "req-private",
            {"ref_request_id": "req-private", "status": "queued"},
        ),
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
        "file_end": Envelope(
            "FILE_END", "req-end", {"transfer_id": transfer_id, "total_chunks": 1}
        ),
        "file_result": Envelope(
            "FILE_RESULT", "req-result", {"transfer_id": transfer_id, "ok": True}
        ),
        "ping": Envelope("PING", "req-ping", {"nonce": "dummy-nonce"}),
        "pong": Envelope("PONG", "req-ping", {"nonce": "dummy-nonce"}),
    }
