"""Shared UDP protocol checks; JSON conversion uses Python's standard json module."""

from typing import Any

UDP_MAX_BYTES = 1200
UDP_PORT = 40001

_FIELDS = {
    "DISCOVER": {"version", "type", "instance_id"},
    "DISCOVER_REPLY": {"version", "type", "instance_id", "server_name", "tcp_port"},
    "HEARTBEAT": {"version", "type", "instance_id", "seq"},
    "HEARTBEAT_ACK": {"version", "type", "instance_id", "seq"},
}


def validate_datagram_size(data: bytes) -> None:
    """Check encoded bytes, before decoding or after encoding."""
    if not 0 < len(data) <= UDP_MAX_BYTES:
        raise ValueError("UDP datagram must contain 1–1200 bytes")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json.loads hook: duplicate keys must be detected before building a dict."""
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate UDP field")
        output[key] = value
    return output


def validate_udp_message(message: object) -> None:
    """Validate protocol fields before encoding or after decoding.

    Request matching (instance ID, sequence and source IP) belongs to the caller.
    """
    if not isinstance(message, dict):
        raise ValueError("UDP message must be a JSON object")
    kind = message.get("type")
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ValueError("Unknown UDP message type")
    if set(message) != _FIELDS[kind]:
        raise ValueError("Invalid UDP message fields")
    if type(message["version"]) is not int or message["version"] != 1:
        raise ValueError("Unsupported UDP version")
    if not isinstance(message["instance_id"], str):
        raise ValueError("Invalid UDP instance ID")
    if kind == "DISCOVER_REPLY":
        if not isinstance(message["server_name"], str) or not message["server_name"]:
            raise ValueError("Invalid UDP server name")
        if type(message["tcp_port"]) is not int or not 1 <= message["tcp_port"] <= 65535:
            raise ValueError("Invalid TCP port")
    if kind in {"HEARTBEAT", "HEARTBEAT_ACK"} and type(message["seq"]) is not int:
        raise ValueError("Invalid heartbeat sequence")
