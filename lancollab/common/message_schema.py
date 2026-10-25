"""DECLARATIVE application payload catalog — design revision 1.1.

C: client->server and S: server->client. Real validation, wire framing,
authorization and temporal checks remain implementer TODOs. This catalog
exists to make mock fixtures, client and server agree on field names.
"""

# All string labels describe types/limits in docs/protocol-spec.md.
# Conditional rule: PRESENCE_UPDATE "online" requires ip and port;
# "offline" MUST omit them. Do not accept bool as an int in validators.
MESSAGE_FIELDS: dict[str, dict[str, dict[str, str]]] = {
    "C:HELLO": {
        "required": {
            "client_name": "str"
        },
        "optional": {}
    },
    "S:HELLO_ACK": {
        "required": {
            "server_name": "str",
            "protocol_version": "int:1"
        },
        "optional": {}
    },
    "C:AUTH_REQUEST": {
        "required": {
            "username": "str:username",
            "password": "str"
        },
        "optional": {}
    },
    "S:AUTH_RESULT": {
        "required": {
            "ok": "bool"
        },
        "optional": {
            "username": "str:username",
            "error_code": "str:error"
        }
    },
    "S:USER_LIST": {
        "required": {
            "users": "list:online_user"
        },
        "optional": {}
    },
    "S:PRESENCE_UPDATE": {
        "required": {
            "username": "str:username",
            "status": "enum:online|offline"
        },
        "optional": {
            "ip": "str:ip",
            "port": "int:port"
        }
    },
    "C:CHAT_GLOBAL": {
        "required": {
            "text": "str:chat"
        },
        "optional": {}
    },
    "S:CHAT_GLOBAL": {
        "required": {
            "from": "str:username",
            "text": "str:chat"
        },
        "optional": {}
    },
    "C:CHAT_PRIVATE": {
        "required": {
            "to": "str:username",
            "text": "str:chat"
        },
        "optional": {}
    },
    "S:CHAT_PRIVATE": {
        "required": {
            "from": "str:username",
            "text": "str:chat"
        },
        "optional": {}
    },
    "S:CHAT_ACK": {
        "required": {
            "status": "enum:queued"
        },
        "optional": {}
    },
    "C:FILE_OFFER": {
        "required": {
            "transfer_id": "str:uuid",
            "to": "str:username",
            "name": "str:filename",
            "size": "int:file_size",
            "sha256": "str:sha256"
        },
        "optional": {}
    },
    "S:FILE_OFFER": {
        "required": {
            "transfer_id": "str:uuid",
            "from": "str:username",
            "name": "str:filename",
            "size": "int:file_size",
            "sha256": "str:sha256"
        },
        "optional": {}
    },
    "C:FILE_ACCEPT": {
        "required": {
            "transfer_id": "str:uuid"
        },
        "optional": {}
    },
    "C:FILE_REJECT": {
        "required": {
            "transfer_id": "str:uuid"
        },
        "optional": {
            "reason": "str:reason"
        }
    },
    "C:FILE_CHUNK": {
        "required": {
            "transfer_id": "str:uuid",
            "seq": "int:nonnegative",
            "offset": "int:nonnegative",
            "data_b64": "str:chunk_b64"
        },
        "optional": {}
    },
    "C:FILE_ACK": {
        "required": {
            "transfer_id": "str:uuid",
            "next_offset": "int:nonnegative"
        },
        "optional": {}
    },
    "C:FILE_END": {
        "required": {
            "transfer_id": "str:uuid",
            "total_chunks": "int:nonnegative"
        },
        "optional": {}
    },
    "C:FILE_RESULT": {
        "required": {
            "transfer_id": "str:uuid",
            "ok": "bool"
        },
        "optional": {
            "reason": "str:reason"
        }
    },
    "C:FILE_CANCEL": {
        "required": {
            "transfer_id": "str:uuid",
            "reason": "str:reason"
        },
        "optional": {}
    },
    "C:PING": {
        "required": {
            "nonce": "str"
        },
        "optional": {}
    },
    "C:PONG": {
        "required": {
            "nonce": "str"
        },
        "optional": {}
    },
    "S:FILE_ACCEPT": {
        "required": {
            "transfer_id": "str:uuid"
        },
        "optional": {}
    },
    "S:FILE_REJECT": {
        "required": {
            "transfer_id": "str:uuid"
        },
        "optional": {
            "reason": "str:reason"
        }
    },
    "S:FILE_CHUNK": {
        "required": {
            "transfer_id": "str:uuid",
            "seq": "int:nonnegative",
            "offset": "int:nonnegative",
            "data_b64": "str:chunk_b64"
        },
        "optional": {}
    },
    "S:FILE_ACK": {
        "required": {
            "transfer_id": "str:uuid",
            "next_offset": "int:nonnegative"
        },
        "optional": {}
    },
    "S:FILE_END": {
        "required": {
            "transfer_id": "str:uuid",
            "total_chunks": "int:nonnegative"
        },
        "optional": {}
    },
    "S:FILE_RESULT": {
        "required": {
            "transfer_id": "str:uuid",
            "ok": "bool"
        },
        "optional": {
            "reason": "str:reason"
        }
    },
    "S:FILE_CANCEL": {
        "required": {
            "transfer_id": "str:uuid",
            "reason": "str:reason"
        },
        "optional": {}
    },
    "S:PING": {
        "required": {
            "nonce": "str"
        },
        "optional": {}
    },
    "S:PONG": {
        "required": {
            "nonce": "str"
        },
        "optional": {}
    },
    "C:LOGOUT": {
        "required": {},
        "optional": {
            "reason": "str:reason"
        }
    },
    "S:ERROR": {
        "required": {
            "code": "str:error",
            "message": "str"
        },
        "optional": {}
    }
}

# Hard design limits; not runtime enforcement until production code is written.
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
}
