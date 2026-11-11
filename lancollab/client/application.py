"""TV3 Issue #7: transport-independent client message/state logic.

Requires a MessageTransport implementation. This module does not establish
TCP/TLS, and the auth_allowed opt-in does not verify TLS. Never set it True
with real credentials until your connection bootstrap has verified TLS.
"""

from dataclasses import dataclass
from enum import Enum
from threading import Event, RLock
from time import monotonic
from pathlib import Path
from typing import Any
from unicodedata import category
from uuid import uuid4
import re
import sqlite3

from lancollab.client.history import HistoryStore

from lancollab.common.contracts import Envelope, MessageTransport
from lancollab.common.codec import ProtocolError
from lancollab.client.transfers import TransferManager, TransferError

_USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
_MAX_CHAT_BYTES = 4096
_MAX_REQUESTS_PER_SESSION = 65536


class ClientState(str, Enum):
    WAIT_AUTH = "WAIT_AUTH"
    WAIT_AUTH_RESULT = "WAIT_AUTH_RESULT"
    AUTHENTICATED = "AUTHENTICATED"
    CLOSED = "CLOSED"
    WAIT_REGISTER_RESULT = "WAIT_REGISTER_RESULT"


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
            rendered.append(f"\\u{n:04x}" if n <= 0xFFFF else f"\\U{n:08x}")
        else:
            rendered.append(char)
    return "".join(rendered)


class CollabClient:
    def __init__(
        self,
        transport: MessageTransport,
        *,
        auth_allowed: bool = False,
        downloads_dir: str | Path = "downloads",
        history: HistoryStore | None = None,
        history_server: str = "",
    ) -> None:
        self.history = history
        self.history_server = history_server
        self.auth_error: str | None = None
        self.registration_result: bool | None = None
        self.transport = transport
        # Real application must set this True ONLY after verified TLS.
        # Tests may set True with FakeTransport and SYNTHETIC passwords only.
        self._auth_allowed = auth_allowed
        self.state = ClientState.WAIT_AUTH
        self.username: str | None = None
        self.role: str | None = None
        self.reply_target: str | None = None
        self.last_tcp_rtt: float | None = None
        # Fresh id on every successful login: quitting and coming back
        # starts a new history session instead of extending the old one.
        self.session_id: str | None = None
        self.pending_users: list[dict[str, str]] = []
        self.issued_token: str | None = None  # set from AUTH_RESULT when remember_me
        self._token_login = False  # True when the current login used a token
        self._requested_username: str | None = None
        self.online_users: dict[str, tuple[str, int]] = {}
        self._pending: dict[str, str] = {}  # request_id -> message type, never passwords
        self._quiet_pending: set[str] = set()  # request_ids whose results stay silent
        self._pending_chat: dict[
            str, tuple[str, str, str | None]
        ] = {}  # request_id -> (kind, text, recipient)
        self._file_requests: dict[str, str] = {}  # request_id -> transfer_id for ERROR
        self._pending_pings: dict[str, tuple[str, float]] = {}
        self.ping_rtts: dict[str, float] = {}
        self._issued_count = 0
        self._request_prefix = uuid4().hex  # random per-client/session prefix
        self._notices: list[Notice] = []
        self._lock = RLock()
        self._send_lock = RLock()  # TV2's writer/transport must serialize frame bytes
        self.files = TransferManager(self._send_file, downloads_dir, notify=self._file_notice)

    def _remember_chat(self, direction, kind, peer, text):
        if self.history is not None and self.username is not None:
            try:
                self.history.append(
                    self.history_server,
                    self.username,
                    direction,
                    kind,
                    peer,
                    text,
                    session=self.session_id or "",
                )
            except (OSError, sqlite3.Error):
                self._notices.append(Notice("error", "Could not save local chat history"))

    def ping(self) -> str:
        with self._lock:
            self._require_authenticated("PING")
            nonce = uuid4().hex
            request = self._new_request("PING", {"nonce": nonce})
            self._pending_pings[request.request_id] = (nonce, monotonic())
        self._send(request)
        return request.request_id

    def _file_notice(self, message: str) -> None:
        with self._lock:
            self._notices.append(Notice("file", safe_display(message)))

    def _send_file(self, kind: str, payload: dict[str, Any]) -> None:
        """File control correlates by transfer_id; no pending direct reply."""
        with self._lock:
            if self.state is not ClientState.AUTHENTICATED:
                raise RuntimeError("Authenticate before file transfer")
            request = self._new_request(kind, payload)
            self._file_requests[request.request_id] = payload["transfer_id"]
        self._send(request)

    def offer_file(self, path: str | Path, recipient: str) -> str:
        with self._lock:
            if self.state is not ClientState.AUTHENTICATED:
                raise RuntimeError("Authenticate before file transfer")
        return self.files.offer(path, recipient)

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

    def login(self, username: str, password: str, *, remember_me: bool = False) -> str:
        if not self._auth_allowed:
            raise RuntimeError("Do not send a password without verified TLS")
        if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
            raise ValueError("Invalid username")
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        with self._lock:
            if self.state is not ClientState.WAIT_AUTH:
                raise RuntimeError("AUTH_REQUEST requires WAIT_AUTH")
            request = self._new_request(
                "AUTH_REQUEST",
                {"username": username, "password": password, "remember_me": remember_me},
            )
            self.auth_error = None
            self._requested_username = username
            self._token_login = False
            self.state = ClientState.WAIT_AUTH_RESULT
        self._send(request)
        return request.request_id

    def login_with_token(self, token: str) -> str:
        """Authenticate with a stored remember-me token, not a password."""
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        with self._lock:
            if self.state is not ClientState.WAIT_AUTH:
                raise RuntimeError("TOKEN_AUTH_REQUEST requires WAIT_AUTH")
            request = self._new_request("TOKEN_AUTH_REQUEST", {"token": token})
            self.auth_error = None
            self._requested_username = None  # the server tells us who this token is
            self._token_login = True
            self.state = ClientState.WAIT_AUTH_RESULT
        self._send(request)
        return request.request_id

    def register(self, username: str, password: str) -> str:
        if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
            raise ValueError("Invalid username")
        if not isinstance(password, str) or len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        with self._lock:
            if self.state is not ClientState.WAIT_AUTH:
                raise RuntimeError("REGISTER_REQUEST requires WAIT_AUTH")
            request = self._new_request(
                "REGISTER_REQUEST", {"username": username, "password": password}
            )
            self._requested_username = username
            self.state = ClientState.WAIT_REGISTER_RESULT
        self._send(request)
        return request.request_id

    def list_pending(self, *, quiet: bool = False) -> str:
        """Ask the server for pending accounts.

        quiet=True is the background sidebar refresh: it updates
        pending_users but prints no "Pending users" notice, so the TUI
        timer does not flood the admin's chat with one line per refresh.
        """
        with self._lock:
            if self.state is not ClientState.AUTHENTICATED or self.role != "admin":
                raise RuntimeError("Only an admin can list pending users")
            request = self._new_request("PENDING_USERS_REQUEST", {})
            if quiet:
                self._quiet_pending.add(request.request_id)
        self._send(request)
        return request.request_id

    def review(self, username: str, decision: str) -> str:
        if decision not in ("approve", "reject"):
            raise ValueError("Decision must be approve or reject")
        if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
            raise ValueError("Invalid username")
        with self._lock:
            if self.state is not ClientState.AUTHENTICATED or self.role != "admin":
                raise RuntimeError("Only an admin can review accounts")
            request = self._new_request(
                "ACCOUNT_REVIEW_REQUEST", {"username": username, "decision": decision}
            )
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
            # Remember what we sent, so CHAT_ACK can echo it back as our own
            # message (the server never echoes a message to its sender).
            self._pending_chat[request.request_id] = (kind, text, payload.get("to"))
        self._send(request)
        return request.request_id

    def send_global(self, text: str) -> str:
        return self._chat("CHAT_GLOBAL", {"text": text}, text)

    def send_private(self, recipient: str, text: str) -> str:
        if not isinstance(recipient, str) or _USERNAME.fullmatch(recipient) is None:
            raise ValueError("Invalid recipient username")
        request_id = self._chat("CHAT_PRIVATE", {"to": recipient, "text": text}, text)
        with self._lock:
            if self.state is ClientState.AUTHENTICATED:
                self.reply_target = recipient
        return request_id

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
        file_event: tuple[str, dict[str, Any]] | None = None
        rejected_file_transfer: str | None = None
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
                if kind == "AUTH_RESULT":
                    if self.state is not ClientState.WAIT_AUTH_RESULT:
                        raise ProtocolViolation("AUTH_RESULT in wrong state")
                    if type(payload.get("ok")) is not bool:
                        raise ProtocolViolation("Invalid AUTH_RESULT.ok")
                    if payload["ok"] is True:
                        if (
                            set(payload)
                            not in ({"ok", "username", "role"}, {"ok", "username", "role", "token"})
                            or not isinstance(payload["username"], str)
                            or (
                                not self._token_login
                                and payload["username"] != self._requested_username
                            )
                            or payload["role"] not in ("member", "admin")
                        ):
                            raise ProtocolViolation("AUTH_RESULT username/role mismatch")
                        self._pop_expected(
                            event, "TOKEN_AUTH_REQUEST" if self._token_login else "AUTH_REQUEST"
                        )
                        self.username = payload["username"]
                        self.files.owner = self.username
                        self.role = payload["role"]
                        self.session_id = uuid4().hex[:12]
                        if self.history is not None:
                            try:
                                self.history.start_session(
                                    self.history_server, self.username, self.session_id
                                )
                            except (OSError, sqlite3.Error):
                                self._notices.append(
                                    Notice("error", "Could not save login session")
                                )
                        self.issued_token = payload.get("token")
                        self._requested_username = None
                        self._token_login = False
                        self.state = ClientState.AUTHENTICATED
                        self._notices.append(Notice("auth", f"Authenticated as {self.role}"))
                    else:
                        if set(payload) != {"ok", "error_code"} or payload["error_code"] not in (
                            "AUTH_FAILED",
                            "ACCOUNT_PENDING",
                            "ACCOUNT_REJECTED",
                            "TOKEN_EXPIRED",
                            "TOKEN_REVOKED",
                        ):
                            raise ProtocolViolation("Invalid failed AUTH_RESULT")
                        self._pop_expected(
                            event, "TOKEN_AUTH_REQUEST" if self._token_login else "AUTH_REQUEST"
                        )
                        self.auth_error = payload["error_code"]
                        self._token_login = False
                        self._requested_username = None
                        self.state = ClientState.WAIT_AUTH
                        self._notices.append(
                            Notice("auth_error", f"Authentication failed: {payload['error_code']}")
                        )

                elif kind == "REGISTER_RESULT":
                    if self.state is not ClientState.WAIT_REGISTER_RESULT:
                        raise ProtocolViolation("REGISTER_RESULT in wrong state")
                    self.registration_result = payload["ok"]
                    self._pop_expected(event, "REGISTER_REQUEST")
                    self._requested_username = None
                    self.state = ClientState.WAIT_AUTH
                    if payload["ok"] is True:
                        self._notices.append(
                            Notice(
                                "auth",
                                "Registered. You can log in now."
                                if payload.get("status") == "active"
                                else "Registered. Waiting for admin approval.",
                            )
                        )
                    else:
                        self._notices.append(
                            Notice(
                                "auth_error", f"Registration failed: {payload.get('error_code')}"
                            )
                        )

                elif kind == "PENDING_USERS_RESULT":
                    self._require_authenticated(kind)
                    self._pop_expected(event, "PENDING_USERS_REQUEST")
                    quiet = event.request_id in self._quiet_pending
                    self._quiet_pending.discard(event.request_id)
                    if set(payload) != {"users"} or not isinstance(payload["users"], list):
                        raise ProtocolViolation("Invalid PENDING_USERS_RESULT")
                    self.pending_users = payload["users"]
                    if not quiet:
                        self._notices.append(
                            Notice(
                                "admin",
                                "Pending users: "
                                + (
                                    ", ".join(
                                        safe_display(u["username"]) for u in self.pending_users
                                    )
                                    or "none"
                                ),
                            )
                        )

                elif kind == "ACCOUNT_REVIEW_RESULT":
                    self._require_authenticated(kind)
                    self._pop_expected(event, "ACCOUNT_REVIEW_REQUEST")
                    if payload["ok"] is True:
                        self._notices.append(
                            Notice("admin", f"{payload['username']} is now {payload['status']}")
                        )
                    else:
                        self._notices.append(
                            Notice("admin", f"Review failed: {payload.get('error_code')}")
                        )

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
                    allowed = {"from", "text"} | ({"queued"} if kind == "CHAT_PRIVATE" else set())
                    if not {"from", "text"} <= set(payload) <= allowed:
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
                    if payload.get("queued") is not None and type(payload["queued"]) is not bool:
                        raise ProtocolViolation("Invalid queued flag")
                    if kind == "CHAT_PRIVATE":
                        self.reply_target = payload["from"]
                    self._remember_chat("received", kind, payload["from"], payload["text"])
                    sender = safe_display(payload["from"])
                    text = safe_display(payload["text"])
                    queued_tag = (
                        " [sent while you were offline]" if payload.get("queued") is True else ""
                    )
                    self._notices.append(Notice("chat", f"[{kind}] {sender}: {text}{queued_tag}"))

                elif kind == "CHAT_ACK":
                    self._require_authenticated(kind)
                    expected = self._pending.get(event.request_id)
                    if expected not in {"CHAT_GLOBAL", "CHAT_PRIVATE"}:
                        raise ProtocolViolation("CHAT_ACK has no pending chat request")
                    if set(payload) != {"status"}:
                        raise ProtocolViolation("Invalid CHAT_ACK fields")
                    if payload.get("status") not in {"queued", "stored"}:
                        raise ProtocolViolation("Unexpected CHAT_ACK status")
                    self._pop_expected(event, expected)
                    sent = self._pending_chat.pop(event.request_id, None)
                    if sent is not None:
                        kind_sent, text_sent, to_sent = sent
                        self._remember_chat("sent", kind_sent, to_sent or "*", text_sent)
                        if kind_sent == "CHAT_PRIVATE":
                            stored_tag = (
                                f" [stored — {safe_display(to_sent or '')} is offline]"
                                if payload.get("status") == "stored"
                                else ""
                            )
                            self._notices.append(
                                Notice(
                                    "chat",
                                    f"[CHAT_PRIVATE] you -> {safe_display(to_sent or '')}: {safe_display(text_sent)}{stored_tag}",
                                )
                            )
                        else:
                            self._notices.append(
                                Notice("chat", f"[CHAT_GLOBAL] you: {safe_display(text_sent)}")
                            )

                elif kind == "ERROR":
                    if set(payload) != {"code", "message"} or not all(
                        isinstance(payload[k], str) for k in ("code", "message")
                    ):
                        raise ProtocolViolation("Invalid ERROR payload")
                    expected = self._pending.pop(event.request_id, None)
                    if expected is None:
                        raise ProtocolViolation("ERROR has no matching request")
                    self._pending_chat.pop(event.request_id, None)
                    code = safe_display(payload["code"])
                    self._notices.append(Notice("error", code))
                    if expected == "AUTH_REQUEST":
                        self._requested_username = None
                        self.state = ClientState.WAIT_AUTH
                    elif expected.startswith("FILE_"):
                        rejected_file_transfer = self._file_requests.pop(event.request_id, None)

                elif isinstance(kind, str) and kind.startswith("FILE_"):
                    self._require_authenticated(kind)
                    file_event = (kind, payload)

                elif kind == "PONG":
                    self._require_authenticated(kind)
                    pending_ping = self._pending_pings.get(event.request_id)
                    if pending_ping is None or payload != {"nonce": pending_ping[0]}:
                        raise ProtocolViolation("PONG does not match outstanding PING")
                    self._pop_expected(event, "PING")
                    self._pending_pings.pop(event.request_id)
                    self.ping_rtts[event.request_id] = (monotonic() - pending_ping[1]) * 1000
                    self.last_tcp_rtt = self.ping_rtts[event.request_id]
                    self._notices.append(
                        Notice("network", f"TCP/TLS RTT: {self.last_tcp_rtt:.1f} ms")
                    )

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
        if rejected_file_transfer is not None:
            self.files.abort(rejected_file_transfer, "Server rejected file request")
        if file_event is not None:
            kind, payload = file_event
            try:
                self.files.handle_event(kind, payload)
            except TransferError as error:
                self._file_notice(f"Rejected {kind}: {error}")
                # Local transfer errors must not take down unrelated chats.
                transfer_id = payload.get("transfer_id")
                if isinstance(transfer_id, str):
                    try:
                        self.files.cancel(transfer_id, "INVALID_TRANSFER_EVENT")
                    except (TransferError, RuntimeError, ConnectionError):
                        pass
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
                    stop.wait(min(poll_timeout, 0.05))
                    continue
                self.handle_event(event)
        except (EOFError, ConnectionError, OSError) as error:
            with self._lock:
                self._notices.append(Notice("disconnected", safe_display(str(error))))
        except (ProtocolViolation, ProtocolError) as error:
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
            if self.history is not None and self.username is not None and self.session_id:
                try:
                    self.history.end_session(self.history_server, self.username, self.session_id)
                except (OSError, sqlite3.Error):
                    self._notices.append(Notice("error", "Could not save session end time"))
            self.state = ClientState.CLOSED
            self.username = None
            self.files.owner = None
            self.role = None
            self.reply_target = None
            self.session_id = None
            self.pending_users = []
            self._requested_username = None
            self.issued_token = None
            self._token_login = False
            self.online_users.clear()
            self._pending.clear()
            self._pending_pings.clear()
            self.ping_rtts.clear()
            self._pending_chat.clear()
            self._file_requests.clear()
        self.files.close()
        self.transport.close()
