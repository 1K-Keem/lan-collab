"""Strict length-prefixed JSON codec for protocol design revision 1.3.

Frame layout: ``uint32_big_endian(N)`` followed by exactly ``N`` bytes of UTF-8 JSON,
``1 <= N <= 262144``. The decoder consumes the declarative catalog in
``message_schema.json`` and enforces types, ranges, conditional presence, direction and
strictness (unknown/duplicate keys, ``bool`` vs ``int``, non-finite numbers).

Error model (protocol-spec section 1):

* :class:`FrameError` -- the frame is unparsable or carries no trustworthy ``request_id``.
  The connection must be closed without attempting an ``ERROR`` reply.
* :class:`MessageError` -- the envelope parsed and has a valid ``request_id`` but the content
  is invalid. Reply with ``ERROR`` using :meth:`ProtocolError.to_error_envelope`; the byte
  stream is still synchronized, so the connection may continue.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import struct
import uuid
from typing import Any, Callable, Literal

from .contracts import Envelope, FILE_CHUNK_BYTES, MAX_JSON_BYTES, PROTOCOL_VERSION
from .message_schema import FIELD_LIMITS, MESSAGE_FIELDS

Role = Literal["client", "server"]

LENGTH_PREFIX = struct.Struct(">I")
MAX_FILE_BYTES = FIELD_LIMITS["file_size_bytes"]
MAX_ONLINE_USERS = 1024

_REQUEST_ID_RE = re.compile(r"[\x21-\x7e]{1,64}")
_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)
_B64_ALPHABET_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}", re.ASCII)
_MAX_B64_CHARS = 4 * ((FILE_CHUNK_BYTES + 2) // 3)


class ProtocolError(Exception):
    """Base class for wire-level protocol violations."""

    def __init__(
        self, code: str, message: str, *, request_id: str | None = None, fatal: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.request_id = request_id
        self.fatal = fatal

    def to_error_envelope(self) -> Envelope | None:
        """Return the direct ``ERROR`` reply, or ``None`` when the connection must just close."""
        if self.fatal or self.request_id is None:
            return None
        return Envelope(
            "ERROR", self.request_id, {"code": self.code, "message": self.public_message[:512]}
        )


class FrameError(ProtocolError):
    """Unparsable frame or untrustworthy request id: close the connection."""

    def __init__(self, message: str, *, code: str = "INVALID_FRAME") -> None:
        super().__init__(code, message, fatal=True)


class MessageError(ProtocolError):
    """Semantically invalid request with a trustworthy ``request_id``: reply with ERROR."""

    def __init__(self, code: str, message: str, *, request_id: str) -> None:
        super().__init__(code, message, request_id=request_id, fatal=False)


class OutboundMessageError(ValueError):
    """Local bug: an outgoing envelope violates the schema. Never sent on the wire."""


class _Invalid(ValueError):
    """Internal validator failure carrying a safe, static reason."""


# --------------------------------------------------------------------------- validators

Validator = Callable[[Any], None]


def _utf8_len(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:  # lone surrogate from a JSON \ud800 escape
        raise _Invalid("not valid Unicode text") from exc


def _str_bytes(min_bytes: int, max_bytes: int) -> Validator:
    def check(value: Any) -> None:
        if not isinstance(value, str):
            raise _Invalid("must be a string")
        size = _utf8_len(value)
        if not min_bytes <= size <= max_bytes:
            raise _Invalid(f"must be {min_bytes}-{max_bytes} UTF-8 bytes")

    return check


def _str_regex(pattern: re.Pattern[str], reason: str) -> Validator:
    def check(value: Any) -> None:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise _Invalid(reason)

    return check


def _check_filename(value: Any) -> None:
    if not isinstance(value, str):
        raise _Invalid("must be a string")
    size = _utf8_len(value)
    if not 1 <= size <= 255:
        raise _Invalid("must be 1-255 UTF-8 bytes")
    if value in {".", ".."}:
        raise _Invalid("must be a plain file name")
    if "/" in value or "\\" in value:
        raise _Invalid("must not contain path separators")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise _Invalid("must not contain control characters")


def _check_uuid(value: Any) -> None:
    if not isinstance(value, str) or len(value) != 36:
        raise _Invalid("must be a canonical UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise _Invalid("must be a canonical UUID string") from exc
    if canonical != value:
        raise _Invalid("must be a canonical lowercase UUID string")


def _check_ip(value: Any) -> None:
    if not isinstance(value, str) or "%" in value or len(value) > 45:
        raise _Invalid("must be an IP address")
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise _Invalid("must be an IP address") from exc


def decoded_b64_length(value: str) -> int:
    """Byte length of a canonical padded base64 string without decoding it."""
    padding = len(value) - len(value.rstrip("="))
    return len(value) // 4 * 3 - padding


def _check_chunk_b64(value: Any) -> None:
    if not isinstance(value, str):
        raise _Invalid("must be a string")
    if not 4 <= len(value) <= _MAX_B64_CHARS or len(value) % 4 != 0:
        raise _Invalid("must be canonical base64 of 1-16384 bytes")
    if _B64_ALPHABET_RE.fullmatch(value) is None:
        raise _Invalid("must be canonical base64 of 1-16384 bytes")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _Invalid("must be canonical base64 of 1-16384 bytes") from exc
    if not 1 <= len(raw) <= FILE_CHUNK_BYTES or base64.b64encode(raw).decode("ascii") != value:
        raise _Invalid("must be canonical base64 of 1-16384 bytes")


def _check_bool(value: Any) -> None:
    if type(value) is not bool:
        raise _Invalid("must be a boolean")


def _int_range(low: int, high: int) -> Validator:
    def check(value: Any) -> None:
        if type(value) is not int:  # excludes bool and floats such as 1.0
            raise _Invalid("must be an integer")
        if not low <= value <= high:
            raise _Invalid(f"must be between {low} and {high}")

    return check


def _int_literal(expected: int) -> Validator:
    def check(value: Any) -> None:
        if type(value) is not int or value != expected:
            raise _Invalid(f"must be the integer {expected}")

    return check


def _enum(values: frozenset[str]) -> Validator:
    def check(value: Any) -> None:
        if not isinstance(value, str) or value not in values:
            raise _Invalid("is not an allowed value")

    return check


_check_username = _str_regex(_USERNAME_RE, "must match [A-Za-z0-9_.-]{1,32}")
_check_port = _int_range(1, 65535)


def _check_online_user(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"username", "ip", "port"}:
        raise _Invalid("entries need exactly username, ip and port")
    _check_username(value["username"])
    _check_ip(value["ip"])
    _check_port(value["port"])


def _check_online_user_list(value: Any) -> None:
    if not isinstance(value, list) or len(value) > MAX_ONLINE_USERS:
        raise _Invalid("must be a list of online users")
    seen: set[str] = set()
    for entry in value:
        _check_online_user(entry)
        if entry["username"] in seen:
            raise _Invalid("contains a duplicate username")
        seen.add(entry["username"])


def _check_pending_user(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"username", "created_at"}:
        raise _Invalid("entries need exactly username and created_at")
    _check_username(value["username"])
    if not isinstance(value["created_at"], str) or not 1 <= len(value["created_at"]) <= 64:
        raise _Invalid("created_at must be a short string")


def _check_pending_user_list(value: Any) -> None:
    if not isinstance(value, list) or len(value) > MAX_ONLINE_USERS:
        raise _Invalid("must be a list of pending users")
    seen: set[str] = set()
    for entry in value:
        _check_pending_user(entry)
        if entry["username"] in seen:
            raise _Invalid("contains a duplicate username")
        seen.add(entry["username"])


_STR_KINDS: dict[str, Validator] = {
    "username": _check_username,
    "chat": _str_bytes(1, FIELD_LIMITS["chat_utf8_bytes"]),
    "filename": _check_filename,
    "uuid": _check_uuid,
    "sha256": _str_regex(_SHA256_RE, "must be 64 lowercase hex characters"),
    "reason": _str_bytes(1, 256),
    "error": _str_regex(_ERROR_CODE_RE, "must be an UPPER_SNAKE_CASE code"),
    "ip": _check_ip,
    "chunk_b64": _check_chunk_b64,
}

_INT_KINDS: dict[str, Validator] = {
    "port": _check_port,
    # Sizes above 50 MiB are valid wire integers so the transfer core can answer
    # FILE_TOO_LARGE rather than a generic INVALID_MESSAGE; 2**53 keeps JSON ints portable.
    "file_size": _int_range(0, 2**53),
    "nonnegative": _int_range(0, MAX_FILE_BYTES),
}

# Plain "str" fields: (min, max) UTF-8 bytes. Unlisted fields use the default.
_PLAIN_STR_LIMITS: dict[str, tuple[int, int]] = {
    "password": (1, 1024),
    "message": (1, 1024),
}
_DEFAULT_PLAIN_STR = (1, 4096)

_NONCE_RE = re.compile(r"[\x21-\x7e]{1,64}")


def _compile_field(field: str, spec: str) -> Validator:
    if spec == "bool":
        return _check_bool
    if spec == "str":
        if field == "nonce":
            return _str_regex(_NONCE_RE, "must be 1-64 printable ASCII characters")
        return _str_bytes(*_PLAIN_STR_LIMITS.get(field, _DEFAULT_PLAIN_STR))
    kind, _, arg = spec.partition(":")
    if kind == "str" and arg in _STR_KINDS:
        return _STR_KINDS[arg]
    if kind == "int":
        if arg in _INT_KINDS:
            return _INT_KINDS[arg]
        if arg.isdigit():
            return _int_literal(int(arg))
    if kind == "enum" and arg:
        return _enum(frozenset(arg.split("|")))
    if spec == "list:online_user":
        return _check_online_user_list
    if spec == "list:pending_user":
        return _check_pending_user_list
    raise KeyError(f"unsupported field spec {spec!r} for {field!r}")


class _CompiledMessage:
    __slots__ = ("required", "optional", "conditional")

    def __init__(self, spec: dict[str, dict[str, str]], conditional: Any) -> None:
        self.required = {name: _compile_field(name, s) for name, s in spec["required"].items()}
        self.optional = {name: _compile_field(name, s) for name, s in spec["optional"].items()}
        self.conditional = conditional


def _auth_result_rules(payload: dict[str, Any]) -> None:
    if payload["ok"]:
        if "username" not in payload or "role" not in payload or "error_code" in payload:
            raise _Invalid("success requires username and role and forbids error_code")
        if payload["role"] not in ("member", "admin"):
            raise _Invalid("role must be member or admin")
    else:
        if "error_code" not in payload or "username" in payload or "role" in payload:
            raise _Invalid("failure requires error_code and forbids username and role")
        if payload["error_code"] not in ("AUTH_FAILED", "ACCOUNT_PENDING", "ACCOUNT_REJECTED"):
            raise _Invalid(
                "failure error_code must be AUTH_FAILED, ACCOUNT_PENDING or ACCOUNT_REJECTED"
            )


def _register_result_rules(payload: dict[str, Any]) -> None:
    if payload["ok"]:
        if payload.get("status") != "pending" or "error_code" in payload:
            raise _Invalid("success requires status pending and forbids error_code")
    else:
        if "error_code" not in payload or "status" in payload:
            raise _Invalid("failure requires error_code and forbids status")


def _review_result_rules(payload: dict[str, Any]) -> None:
    if payload["ok"]:
        if "username" not in payload or "status" not in payload or "error_code" in payload:
            raise _Invalid("success requires username and status and forbids error_code")
    else:
        if "error_code" not in payload or "username" in payload or "status" in payload:
            raise _Invalid("failure requires error_code and forbids username and status")


def _presence_rules(payload: dict[str, Any]) -> None:
    has_endpoint = "ip" in payload or "port" in payload
    if payload["status"] == "online":
        if "ip" not in payload or "port" not in payload:
            raise _Invalid("online requires ip and port")
    elif has_endpoint:
        raise _Invalid("offline forbids ip and port")


_CONDITIONAL_RULES: dict[str, Callable[[dict[str, Any]], None]] = {
    "S:AUTH_RESULT": _auth_result_rules,
    "S:REGISTER_RESULT": _register_result_rules,
    "S:ACCOUNT_REVIEW_RESULT": _review_result_rules,
    "S:PRESENCE_UPDATE": _presence_rules,
}

_COMPILED: dict[str, _CompiledMessage] = {
    key: _CompiledMessage(spec, _CONDITIONAL_RULES.get(key)) for key, spec in MESSAGE_FIELDS.items()
}  # fails at import time if the catalog uses a spec this codec does not implement


def _validate_payload(key: str, payload: dict[str, Any], request_id: str) -> None:
    compiled = _COMPILED[key]
    keys = set(payload)
    allowed = set(compiled.required) | set(compiled.optional)
    if not set(compiled.required) <= keys or not keys <= allowed:
        raise MessageError(
            "INVALID_MESSAGE", "payload has missing or unknown fields", request_id=request_id
        )
    try:
        for name, value in payload.items():
            check = compiled.required.get(name) or compiled.optional[name]
            try:
                check(value)
            except _Invalid as exc:
                raise _Invalid(f"field '{name}' {exc}") from exc
        if compiled.conditional is not None:
            compiled.conditional(payload)
    except _Invalid as exc:
        raise MessageError("INVALID_MESSAGE", str(exc), request_id=request_id) from exc


def is_valid_request_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def known_types(direction: Literal["C", "S"]) -> frozenset[str]:
    prefix = direction + ":"
    return frozenset(key[len(prefix) :] for key in MESSAGE_FIELDS if key.startswith(prefix))


# ------------------------------------------------------------------------------ codec

_ENVELOPE_KEYS = frozenset({"version", "type", "request_id", "payload"})


def _parse_json(body: bytes) -> tuple[Any, bool]:
    """Strictly parse a JSON body; returns ``(object, had_duplicate_keys)``."""
    duplicates: list[str] = []

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    def reject_constant(name: str) -> Any:
        raise ValueError(f"non-finite number {name}")

    try:
        text = body.decode("utf-8")
        obj = json.loads(text, object_pairs_hook=pairs_hook, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FrameError("frame body is not valid UTF-8 JSON") from exc
    return obj, bool(duplicates)


class JsonFrameCodec:
    """Encode/decode complete frames for one side of the connection.

    ``role="server"`` decodes ``C:`` messages and encodes ``S:`` messages; ``role="client"``
    is the mirror image. Instances are stateless and safe to share between threads.
    """

    def __init__(self, role: Role) -> None:
        if role not in ("client", "server"):
            raise ValueError("role must be 'client' or 'server'")
        self.role: Role = role
        self._in: Literal["C", "S"] = "C" if role == "server" else "S"
        self._out: Literal["C", "S"] = "S" if role == "server" else "C"
        self._in_types = known_types(self._in)

    # ------------------------------------------------------------------ encoding
    def encode_body(self, message: Envelope) -> bytes:
        if type(message.version) is not int or message.version != PROTOCOL_VERSION:
            raise OutboundMessageError("unsupported envelope version")
        if not is_valid_request_id(message.request_id):
            raise OutboundMessageError("invalid request_id")
        key = f"{self._out}:{message.type}"
        if key not in _COMPILED:
            raise OutboundMessageError(f"{message.type!r} cannot be sent by a {self.role}")
        if not isinstance(message.payload, dict):
            raise OutboundMessageError("payload must be an object")
        try:
            _validate_payload(key, message.payload, message.request_id)
        except MessageError as exc:
            raise OutboundMessageError(exc.public_message) from exc
        try:
            body = json.dumps(
                {
                    "version": message.version,
                    "type": message.type,
                    "request_id": message.request_id,
                    "payload": message.payload,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise OutboundMessageError("payload is not JSON serializable") from exc
        if not 1 <= len(body) <= MAX_JSON_BYTES:
            raise OutboundMessageError("encoded message exceeds the frame limit")
        return body

    def encode(self, message: Envelope) -> bytes:
        """Return ``length prefix + JSON body`` ready for a single serialized write."""
        body = self.encode_body(message)
        return LENGTH_PREFIX.pack(len(body)) + body

    # ------------------------------------------------------------------ decoding
    def decode(self, frame: bytes) -> Envelope:
        """Decode one complete frame including its 4-byte length prefix."""
        if len(frame) < LENGTH_PREFIX.size:
            raise FrameError("incomplete length prefix")
        (length,) = LENGTH_PREFIX.unpack_from(frame)
        check_frame_length(length)
        if len(frame) - LENGTH_PREFIX.size != length:
            raise FrameError("frame length does not match its prefix")
        return self.decode_body(bytes(frame[LENGTH_PREFIX.size :]))

    def decode_body(self, body: bytes) -> Envelope:
        """Decode and fully validate a JSON body whose length prefix is already consumed."""
        obj, duplicate_keys = _parse_json(body)
        if not isinstance(obj, dict):
            raise FrameError("envelope must be a JSON object")
        request_id = obj.get("request_id")
        if not is_valid_request_id(request_id):
            raise FrameError("missing or invalid request_id")
        assert isinstance(request_id, str)

        def reject(code: str, message: str) -> MessageError:
            return MessageError(code, message, request_id=request_id)

        if duplicate_keys:
            raise reject("INVALID_MESSAGE", "duplicate JSON keys")
        if set(obj) != _ENVELOPE_KEYS:
            raise reject("INVALID_MESSAGE", "envelope needs exactly the four standard keys")
        version = obj["version"]
        if type(version) is not int:
            raise reject("INVALID_MESSAGE", "version must be an integer")
        if version != PROTOCOL_VERSION:
            raise reject("UNSUPPORTED_VERSION", "unsupported protocol version")
        msg_type = obj["type"]
        if not isinstance(msg_type, str) or msg_type not in self._in_types:
            raise reject("INVALID_MESSAGE", "unsupported message type for this direction")
        payload = obj["payload"]
        if not isinstance(payload, dict):
            raise reject("INVALID_MESSAGE", "payload must be an object")
        _validate_payload(f"{self._in}:{msg_type}", payload, request_id)
        return Envelope(type=msg_type, request_id=request_id, payload=payload, version=version)


def check_frame_length(length: int) -> None:
    """Validate a decoded length prefix *before* any body allocation or read."""
    if not 1 <= length <= MAX_JSON_BYTES:
        raise FrameError("frame length outside 1-262144 bytes")


# ------------------------------------------------------------------ request-id history


class RequestIdTracker:
    """Per-session history of client request ids (protocol-spec section 1).

    Duplicate ids are rejected without re-executing the request. History is bounded; on
    exhaustion the session must close (ids are never evicted, so replays cannot be accepted).
    ``PONG`` replies echo the server's PING id and must not be registered here.
    """

    def __init__(self, limit: int = FIELD_LIMITS["max_request_ids_per_session"]) -> None:
        self._limit = limit
        self._seen: set[str] = set()

    def __len__(self) -> int:
        return len(self._seen)

    def register(self, request_id: str) -> None:
        if request_id in self._seen:
            raise MessageError("INVALID_MESSAGE", "duplicate request_id", request_id=request_id)
        if len(self._seen) >= self._limit:
            raise FrameError("request id history exhausted; reconnect", code="INVALID_STATE")
        self._seen.add(request_id)
