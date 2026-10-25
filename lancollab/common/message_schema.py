"""Declarative field catalog for the v1.2 design (not a runtime validator).

Schema is data, shared by codec implementations and mock tests. Full state,
type/range validation and authorization remain student implementation TODOs.
"""

import json
from pathlib import Path

# A JSON file avoids duplicating message names across Python and documentation.
MESSAGE_FIELDS: dict[str, dict[str, dict[str, str]]] = json.loads(
    Path(__file__).with_name("message_schema.json").read_text(encoding="utf-8")
)

FIELD_LIMITS = {
    "json_body_bytes": 262_144,
    "file_chunk_bytes": 16 * 1024,
    "file_size_bytes": 50 * 1024 * 1024,
    "username_chars": 32,
    "chat_utf8_bytes": 4096,
    "filename_utf8_bytes": 255,
    "request_id_chars": 64,
    "max_file_window_chunks": 8,
    "file_ack_interval_chunks": 4,
    "control_queue_frames": 128,
    "file_queue_frames": 32,
    "max_seen_transfer_ids_per_session": 1024,
    "max_request_ids_per_session": 65536,
}
