"""TV3 Issue #7: transport-independent client message/state logic.

Requires a MessageTransport implementation. This module does not establish
TCP/TLS, and the auth_allowed opt-in does not verify TLS. Never set it True
with real credentials until your connection bootstrap has verified TLS.
"""

from dataclasses import dataclass
from enum import Enum
from threading import Event, RLock
from typing import Any
from unicodedata import category
from uuid import uuid4
import re

from lancollab.common.contracts import Envelope, MessageTransport

_USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
_MAX_CHAT_BYTES = 4096
_MAX_REQUESTS_PER_SESSION = 65536


class ClientState(str, Enum):
    READY = "READY"                   # usable transport is supplied
    WAIT_HELLO_ACK = "WAIT_HELLO_ACK"
    WAIT_AUTH = "WAIT_AUTH"
    WAIT_AUTH_RESULT = "WAIT_AUTH_RESULT"
    AUTHENTICATED = "AUTHENTICATED"
    CLOSED = "CLOSED"


class ProtocolViolation(Exception):
    """Server event conflicts with the expected client state/contract."""


@dataclass(frozen=True)
class Notice:
    kind: str
    text: str


def safe_display(text: str) -> str:
    """Render untrusted text without ANSI/control/format sequences.

    Control/invisible format code points are visibly escaped rather than
    interpreted by the terminal. Keep Unicode letters and printable text.
    """
    if not isinstance(text, str):
        raise TypeError("display text must be a string")
    rendered: list[str] = []
    for char in text:
        if category(char) in {"Cc", "Cf", "Cs"}:
            n = ord(char)
            rendered.append(f"\\u{n:04x}" if n <= 0xffff else f"\\U{n:08x}")
        else:
            rendered.append(char)
    return "".join(rendered)


class CollabClient:
    def __init__(
        self,
        transport: MessageTransport,
        *,
        auth_allowed: bool = False,
    ) -> None:
        self.transport = transport
        # Real application must set this True ONLY after verified TLS.
        # Tests may set True with FakeTransport and SYNTHETIC passwords only.
        self._auth_allowed = auth_allowed
        self.state = ClientState.READY
        self.username: str | None = None
        self._requested_username: str | None = None
        self.online_users: dict[str, tuple[str, int]] = {}
        self._pending: dict[str, str] = {}  # request_id -> message type, never passwords
        self._issued_count = 0
        self._request_prefix = uuid4().hex  # random per-client/session prefix
        self._notices: list[Notice] = []
        self._lock = RLock()
        self._send_lock = RLock()  # TV2's writer/transport must serialize frame bytes

    def _new_request(self, kind: str, payload: dict[str, Any]) -> Envelope:
        if self._issued_count >= _MAX_REQUESTS_PER_SESSION:
            raise RuntimeError("Request ID/session cap reached; reconnect required")
        self._issued_count += 1
        request_id = f"{self._request_prefix}-{self._issued_count}"
        self._pending[request_id] = kind
        return Envelope(type=kind, request_id=request_id, payload=payload)

    def _send(self, message: Envelope) -> None:
        try:
            with self._send_lock:
                self.transport.send(message)
        except Exception:
            self.close()
            raise

    def send_hello(self, client_name: str) -> str:
        if not isinstance(client_name, str) or not client_name.strip():
            raise ValueError("client_name must be a non-empty string")
        with self._lock:
            if self.state is not ClientState.READY:
                raise RuntimeError("HELLO is only allowed once on a new connection")
            request = self._new_request("HELLO", {"client_name": client_name})
            self.state = ClientState.WAIT_HELLO_ACK
        self._send(request)
        return request.request_id

    def login(self, username: str, password: str) -> str:
        if not self._auth_allowed:
            raise RuntimeError("Do not send a password without verified TLS")
        if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
            raise ValueError("Invalid username")
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        with self._lock:
            if self.state is not ClientState.WAIT_AUTH:
                raise RuntimeError("Wait for HELLO_ACK before AUTH_REQUEST")
            request = self._new_request(
                "AUTH_REQUEST", {"username": username, "password": password}
            )
            self._requested_username = username
            self.state = ClientState.WAIT_AUTH_RESULT
        self._send(request)
        return request.request_id

    def logout(self) -> None:
        """Request graceful closure; no server response is expected."""
        with self._lock:
            if self.state not in {ClientState.WAIT_AUTH, ClientState.AUTHENTICATED}:
                raise RuntimeError("LOGOUT requires WAIT_AUTH or AUTHENTICATED")
            request = self._new_request("LOGOUT", {})
            # LOGOUT has no direct reply in protocol v1; do not leak pending.
            self._pending.pop(request.request_id)
        try:
            self._send(request)
        finally:
            self.close()

    def _chat(self, kind: str, payload: dict[str, Any], text: str) -> str:
        if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= _MAX_CHAT_BYTES:
            raise ValueError("Chat must be 1–4096 UTF-8 bytes")
        with self._lock:
            if self.state is not ClientState.AUTHENTICATED:
                raise RuntimeError("Authenticate before chatting")
            request = self._new_request(kind, payload)
        self._send(request)
        return request.request_id

    def send_global(self, text: str) -> str:
        return self._chat("CHAT_GLOBAL", {"text": text}, text)

    def send_private(self, recipient: str, text: str) -> str:
        if not isinstance(recipient, str) or _USERNAME.fullmatch(recipient) is None:
            raise ValueError("Invalid recipient username")
        return self._chat("CHAT_PRIVATE", {"to": recipient, "text": text}, text)

    def _pop_expected(self, event: Envelope, expected: str) -> None:
        if self._pending.get(event.request_id) != expected:
            raise ProtocolViolation(f"Unexpected {event.type} request_id")
        del self._pending[event.request_id]

    def handle_event(self, event: Envelope) -> None:
        """Handle exactly one validated server event; no blocking I/O under state lock.

        The production codec (TV2) must validate message schema before this method.
        Tests with FakeTransport exercise state transitions, not codec validation.
        """
        reply: Envelope | None = None
        should_close = False
        try:
            with self._lock:
                if self.state is ClientState.CLOSED:
                    return
                # Defense in depth for test doubles; TV2's production codec is
                # responsible for complete wire-schema and frame validation.
                if not isinstance(event, Envelope):
                    raise ProtocolViolation("Expected Envelope")
                if type(event.version) is not int or event.version != 1:
                    raise ProtocolViolation("Unsupported envelope version")
                if (
                    not isinstance(event.request_id, str)
                    or not 1 <= len(event.request_id) <= 64
                    or not all(33 <= ord(c) <= 126 for c in event.request_id)
                ):
                    raise ProtocolViolation("Invalid request_id")
                if not isinstance(event.payload, dict):
                    raise ProtocolViolation("Invalid payload")
                kind, payload = event.type, event.payload
                if kind == "HELLO_ACK":
                    if self.state is not ClientState.WAIT_HELLO_ACK:
                        raise ProtocolViolation("HELLO_ACK in wrong state")
                    if (
                        set(payload) != {"server_name", "protocol_version"}
                        or not isinstance(payload["server_name"], str)
                        or type(payload["protocol_version"]) is not int
                        or payload["protocol_version"] != 1
                    ):
                        raise ProtocolViolation("Invalid HELLO_ACK payload")
                    self._pop_expected(event, "HELLO")
                    self.state = ClientState.WAIT_AUTH
                    self._notices.append(Notice("hello", "Server handshake acknowledged"))

                elif kind == "AUTH_RESULT":
                    if self.state is not ClientState.WAIT_AUTH_RESULT:
                        raise ProtocolViolation("AUTH_RESULT in wrong state")
                    if type(payload.get("ok")) is not bool:
                        raise ProtocolViolation("Invalid AUTH_RESULT.ok")
                    if payload["ok"] is True:
                        if (
                            set(payload) != {"ok", "username"}
                            or not isinstance(payload["username"], str)
                            or payload["username"] != self._requested_username
                        ):
                            raise ProtocolViolation("AUTH_RESULT username mismatch")
                        self._pop_expected(event, "AUTH_REQUEST")
                        self.username = self._requested_username
                        self._requested_username = None
                        self.state = ClientState.AUTHENTICATED
                        self._notices.append(Notice("auth", "Authenticated"))
                    else:
                        if (
                            set(payload) != {"ok", "error_code"}
                            or payload["error_code"] != "AUTH_FAILED"
                        ):
                            raise ProtocolViolation("Invalid failed AUTH_RESULT")
                        self._pop_expected(event, "AUTH_REQUEST")
                        self._requested_username = None
                        self.state = ClientState.WAIT_AUTH
                        self._notices.append(Notice("auth_error", "Authentication failed"))

                elif kind == "USER_LIST":
                    self._require_authenticated(kind)
                    if set(payload) != {"users"} or not isinstance(payload["users"], list):
                        raise ProtocolViolation("Invalid USER_LIST")
                    users = payload["users"]
                    snapshot: dict[str, tuple[str, int]] = {}
                    for user in users:
                        if not isinstance(user, dict) or set(user) != {"username", "ip", "port"}:
                            raise ProtocolViolation("Invalid USER_LIST user")
                        self._check_online_user(user["username"], user["ip"], user["port"])
                        if user["username"] in snapshot:
                            raise ProtocolViolation("Duplicate user in USER_LIST")
                        snapshot[user["username"]] = (user["ip"], user["port"])
                    self.online_users = snapshot
                    self._notices.append(Notice("presence", "Online list refreshed"))

                elif kind == "PRESENCE_UPDATE":
                    self._require_authenticated(kind)
                    username, status = payload["username"], payload["status"]
                    if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
                        raise ProtocolViolation("Invalid presence username")
                    if status == "online":
                        if set(payload) != {"username", "status", "ip", "port"}:
                            raise ProtocolViolation("Invalid online presence fields")
                        self._check_online_user(username, payload["ip"], payload["port"])
                        self.online_users[username] = (payload["ip"], payload["port"])
                    elif status == "offline":
                        if set(payload) != {"username", "status"}:
                            raise ProtocolViolation("Invalid offline presence fields")
                        self.online_users.pop(username, None)
                    else:
                        raise ProtocolViolation("Invalid presence status")
                    self._notices.append(Notice("presence", f"{safe_display(username)}: {status}"))

                elif kind in {"CHAT_GLOBAL", "CHAT_PRIVATE"}:
                    self._require_authenticated(kind)
                    if set(payload) != {"from", "text"}:
                        raise ProtocolViolation("Invalid routed chat fields")
                    if (
                        not isinstance(payload["from"], str)
                        or _USERNAME.fullmatch(payload["from"]) is None
                    ):
                        raise ProtocolViolation("Invalid chat sender")
                    if (
                        not isinstance(payload["text"], str)
                        or not 1 <= len(payload["text"].encode("utf-8")) <= _MAX_CHAT_BYTES
                    ):
                        raise ProtocolViolation("Invalid chat text")
                    sender = safe_display(payload["from"])
                    text = safe_display(payload["text"])
                    self._notices.append(Notice("chat", f"[{kind}] {sender}: {text}"))

                elif kind == "CHAT_ACK":
                    self._require_authenticated(kind)
                    expected = self._pending.get(event.request_id)
                    if expected not in {"CHAT_GLOBAL", "CHAT_PRIVATE"}:
                        raise ProtocolViolation("CHAT_ACK has no pending chat request")
                    if set(payload) != {"status"}:
                        raise ProtocolViolation("Invalid CHAT_ACK fields")
                    if payload.get("status") != "queued":
                        raise ProtocolViolation("Unexpected CHAT_ACK status")
                    self._pop_expected(event, expected)
                    self._notices.append(
                        Notice("chat_queued", "Queued on server; not delivered/read")
                    )

                elif kind == "ERROR":
                    if set(payload) != {"code", "message"} or not all(
                        isinstance(payload[k], str) for k in ("code", "message")
                    ):
                        raise ProtocolViolation("Invalid ERROR payload")
                    expected = self._pending.pop(event.request_id, None)
                    if expected is None:
                        raise ProtocolViolation("ERROR has no matching request")
                    code = safe_display(payload["code"])
                    self._notices.append(Notice("error", code))
                    if expected == "AUTH_REQUEST":
                        self._requested_username = None
                        self.state = ClientState.WAIT_AUTH
                    elif expected == "HELLO":
                        should_close = True

                elif kind == "PING":
                    self._require_authenticated(kind)
                    if set(payload) != {"nonce"} or not isinstance(payload["nonce"], str):
                        raise ProtocolViolation("Invalid PING payload")
                    reply = Envelope("PONG", event.request_id, {"nonce": payload["nonce"]})

                else:
                    raise ProtocolViolation(f"Unexpected server event: {kind}")
        except (ProtocolViolation, KeyError, TypeError, ValueError) as error:
            self.close()
            if isinstance(error, ProtocolViolation):
                raise
            raise ProtocolViolation("Invalid server event payload") from error
        if should_close:
            self.close()
        if reply is not None:
            self._send(reply)

    @staticmethod
    def _check_online_user(username: object, ip: object, port: object) -> None:
        if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
            raise ProtocolViolation("Invalid online username")
        if not isinstance(ip, str) or not ip:
            raise ProtocolViolation("Invalid presence IP")
        if type(port) is not int or not (1 <= port <= 65535):
            raise ProtocolViolation("Invalid presence port")

    def receive_loop(self, stop: Event, *, poll_timeout: float = 0.25) -> None:
        """Use with an actual blocking MessageTransport; FakeTransport won't block.

        A receive timeout is a normal polling wakeup. EOF/errors terminate
        the current session; stale events must not revive CLOSED state.
        """
        if poll_timeout <= 0:
            raise ValueError("poll_timeout must be positive")
        try:
            while not stop.is_set():
                with self._lock:
                    if self.state is ClientState.CLOSED:
                        return
                try:
                    event = self.transport.receive(timeout=poll_timeout)
                except TimeoutError:
                    continue
                self.handle_event(event)
        except (EOFError, ConnectionError, OSError) as error:
            with self._lock:
                self._notices.append(Notice("disconnected", safe_display(str(error))))
        except ProtocolViolation as error:
            with self._lock:
                self._notices.append(Notice("protocol_error", safe_display(str(error))))
        finally:
            self.close()

    def _require_authenticated(self, kind: str) -> None:
        if self.state is not ClientState.AUTHENTICATED:
            raise ProtocolViolation(f"{kind} before authentication")

    def drain_notices(self) -> list[Notice]:
        with self._lock:
            notices, self._notices = self._notices, []
            return notices

    def close(self) -> None:
        with self._lock:
            if self.state is ClientState.CLOSED:
                return
            self.state = ClientState.CLOSED
            self.username = None
            self._requested_username = None
            self.online_users.clear()
            self._pending.clear()
        self.transport.close()