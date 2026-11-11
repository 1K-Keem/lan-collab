"""TV1 server handler: auth, sessions, presence, chat routing.

No networking here. handle_message() returns OutboundEvents; the
connection layer in connections.py delivers them to the right transports.
"""

from __future__ import annotations

import uuid
from functools import wraps
from threading import RLock

from lancollab.common.transfer_core import TransferRegistry
from lancollab.common.contracts import Envelope, OutboundEvent, SessionIdentity
from lancollab.server.accounts import AccountError, AccountService
from lancollab.server.queue import MessageQueue
from lancollab.server.tokens import TokenService

MAX_FAILED_ATTEMPTS = 3


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapped


class CollabServer:
    def __init__(
        self,
        accounts: AccountService,
        tokens: TokenService | None = None,
        queue: MessageQueue | None = None,
        *,
        auto_approve: bool = False,
    ) -> None:
        self.lock = RLock()
        self.transfers = TransferRegistry()
        self._purges: set[str] = set()
        self.auto_approve = auto_approve
        self.accounts = accounts
        self.tokens = tokens
        self.queue = queue
        self._sessions: dict[str, SessionIdentity] = {}  # session_id -> identity
        self._by_username: dict[str, str] = {}  # username -> session_id
        self._failed: dict[str, int] = {}  # session_id -> failed auth attempts
        self._session_tokens: dict[str, bytes] = {}  # session_id -> token_hash
        self._close_after: set[str] = set()  # session_ids to close after delivery

    # -- helpers ---------------------------------------------------------
    @synchronized
    def session_for(self, session_id: str) -> SessionIdentity | None:
        return self._sessions.get(session_id)

    @synchronized
    def pop_closed_sessions(self) -> list[str]:
        closed, self._close_after = sorted(self._close_after), set()
        return closed

    def _reply(
        self, session: SessionIdentity, kind: str, request_id: str, payload: dict
    ) -> OutboundEvent:
        return OutboundEvent(session, Envelope(kind, request_id, payload))

    def _error(self, session: SessionIdentity, request_id: str, code: str) -> OutboundEvent:
        return self._reply(session, "ERROR", request_id, {"code": code, "message": code})

    def _user_list(self, session: SessionIdentity) -> OutboundEvent:
        users = [
            {"username": ident.username, "ip": ident.remote_ip, "port": ident.remote_port}
            for ident in self._sessions.values()
            if ident.username is not None
        ]
        return OutboundEvent(session, Envelope("USER_LIST", uuid.uuid4().hex, {"users": users}))

    def _presence(
        self, ident: SessionIdentity, status: str, exclude: str | None = None
    ) -> list[OutboundEvent]:
        payload: dict = {"username": ident.username, "status": status}
        if status == "online":
            payload.update({"ip": ident.remote_ip, "port": ident.remote_port})
        return [
            OutboundEvent(other, Envelope("PRESENCE_UPDATE", uuid.uuid4().hex, dict(payload)))
            for other in self._sessions.values()
            if other.session_id != exclude and other.username is not None
        ]

    def _takeover(self, username: str, new_session_id: str) -> list[str]:
        old_id = self._by_username.get(username)
        if old_id is not None and old_id != new_session_id:
            self._sessions.pop(old_id, None)
            self._failed.pop(old_id, None)
            return [old_id]
        return []

    def _login_success(
        self, session: SessionIdentity, username: str, role: str, rid: str, token: str | None = None
    ) -> list[OutboundEvent]:
        """Create the session, send AUTH_RESULT (+ optional token) and USER_LIST."""
        self._failed.pop(session.session_id, None)
        closed = self._takeover(username, session.session_id)
        self._close_after.update(closed)
        ident = SessionIdentity(
            session.remote_ip, session.remote_port, session.session_id, username, role
        )
        self.transfers.open_session(ident)
        self._sessions[ident.session_id] = ident
        self._by_username[username] = ident.session_id
        payload: dict = {"ok": True, "username": username, "role": role}
        if token is not None:
            payload["token"] = token
        events = [
            self._reply(ident, "AUTH_RESULT", rid, payload),
            self._user_list(ident),
        ]
        for old_id in closed:
            events += self.transfer_events(self.transfers.close_session(old_id, "SESSION_REPLACED"))
        events += self._drain_queue(ident)
        return events + self._presence(ident, "online", exclude=ident.session_id)

    def _store_offline(self, sender: str | None, payload: dict) -> bool:
        """Store a private message for an offline recipient.

        Returns True if stored. Only existing *active* accounts get a
        queue: unknown users stay USER_OFFLINE (no account to deliver
        to), pending users cannot log in yet, and rejected users must
        never receive anything. Files are never stored.
        """
        if self.queue is None or sender is None:
            return False
        account = self.accounts.get_account(payload["to"])
        if account is None or account["status"] != "active":
            return False
        return self.queue.enqueue(payload["to"], sender, "CHAT_PRIVATE", {"text": payload["text"]})

    def _drain_queue(self, ident: SessionIdentity) -> list[OutboundEvent]:
        """Deliver messages stored while ``ident`` was offline, oldest first.

        Only CHAT_PRIVATE is ever queued. The ``queued`` flag lets the
        client show the message was sent while the recipient was away.
        Drain deletes on read (at-most-once delivery).
        """
        if self.queue is None or ident.username is None:
            return []
        events = []
        for item in self.queue.drain(ident.username):
            if item["kind"] != "CHAT_PRIVATE":
                continue
            events.append(
                OutboundEvent(
                    ident,
                    Envelope(
                        "CHAT_PRIVATE",
                        uuid.uuid4().hex,
                        {
                            "from": item["sender"],
                            "text": item["payload"]["text"],
                            "queued": True,
                        },
                    ),
                )
            )
        return events

    def _login_failed(
        self, session: SessionIdentity, rid: str, error_code: str
    ) -> list[OutboundEvent]:
        self._failed[session.session_id] = self._failed.get(session.session_id, 0) + 1
        if self._failed[session.session_id] >= MAX_FAILED_ATTEMPTS:
            self._close_after.add(session.session_id)
        return [self._reply(session, "AUTH_RESULT", rid, {"ok": False, "error_code": error_code})]

    # -- lifecycle -------------------------------------------------------
    @synchronized
    def close_session(self, session_id: str) -> list[OutboundEvent]:
        events = self.transfer_events(self.transfers.close_session(session_id))
        ident = self._sessions.pop(session_id, None)
        self._failed.pop(session_id, None)
        self._session_tokens.pop(session_id, None)
        if ident is None or ident.username is None:
            return events
        if self._by_username.get(ident.username) == session_id:
            self._by_username.pop(ident.username, None)
            events += self._presence(ident, "offline", exclude=session_id)
        return events

    def transfer_events(self, outcome, session=None, request_id=None):
        self._purges.update(outcome.purge)
        events = [OutboundEvent(f.recipient, f.message) for f in outcome.forwards]
        if outcome.error is not None and session is not None:
            events.insert(0, OutboundEvent(session, outcome.error.reply(request_id)))
        return events

    def pop_purges(self):
        purges, self._purges = self._purges, set()
        return purges

    # -- dispatch --------------------------------------------------------
    @synchronized
    def handle_message(self, message: Envelope, session: SessionIdentity) -> list[OutboundEvent]:
        kind, payload, rid = message.type, message.payload, message.request_id
        authed = self._sessions.get(session.session_id)

        if kind == "REGISTER_REQUEST":
            if authed is not None:
                return [self._error(session, rid, "INVALID_STATE")]
            try:
                self.accounts.create_account(
                    payload["username"], payload["password"], bootstrap=self.auto_approve
                )
            except AccountError as exc:
                return [
                    self._reply(
                        session, "REGISTER_RESULT", rid, {"ok": False, "error_code": exc.code}
                    )
                ]
            self._close_after.add(session.session_id)  # close after delivery; log in later
            return [
                self._reply(
                    session,
                    "REGISTER_RESULT",
                    rid,
                    {"ok": True, "status": "active" if self.auto_approve else "pending"},
                )
            ]

        if kind == "AUTH_REQUEST":
            if authed is not None:
                return [self._error(session, rid, "INVALID_STATE")]
            if self.auto_approve and self.accounts.get_account(payload["username"]) is None:
                try:
                    self.accounts.create_account(
                        payload["username"], payload["password"], bootstrap=True
                    )
                except AccountError as error:
                    if error.code != "USERNAME_TAKEN":
                        return self._login_failed(session, rid, "AUTH_FAILED")
            ok, error_code, role = self.accounts.authenticate(
                payload["username"], payload["password"], auto_approve=self.auto_approve
            )
            if not ok:
                return self._login_failed(session, rid, error_code)
            token = None
            if payload.get("remember_me") is True and self.tokens is not None:
                token = self.tokens.issue(payload["username"])
                from hashlib import sha256 as _sha256

                self._session_tokens[session.session_id] = _sha256(token.encode()).digest()
            return self._login_success(session, payload["username"], role, rid, token=token)

        if kind == "TOKEN_AUTH_REQUEST":
            if authed is not None:
                return [self._error(session, rid, "INVALID_STATE")]
            if self.tokens is None:
                return self._login_failed(session, rid, "AUTH_FAILED")
            username, error_code = self.tokens.verify(payload["token"])
            if error_code is not None:
                return self._login_failed(session, rid, error_code)
            account = self.accounts.get_account(username)
            if account is None:
                return self._login_failed(session, rid, "AUTH_FAILED")
            if account["status"] == "pending":
                return self._login_failed(session, rid, "ACCOUNT_PENDING")
            if account["status"] == "rejected":
                return self._login_failed(session, rid, "ACCOUNT_REJECTED")
            from hashlib import sha256 as _sha256

            self._session_tokens[session.session_id] = _sha256(payload["token"].encode()).digest()
            return self._login_success(session, username, account["role"], rid)

        if kind == "PENDING_USERS_REQUEST":
            if authed is None or authed.role != "admin":
                return [self._error(session, rid, "NOT_AUTHORIZED")]
            return [
                self._reply(
                    session, "PENDING_USERS_RESULT", rid, {"users": self.accounts.list_pending()}
                )
            ]

        if kind == "ACCOUNT_REVIEW_REQUEST":
            if authed is None or authed.role != "admin":
                return [self._error(session, rid, "NOT_AUTHORIZED")]
            try:
                status = self.accounts.review(
                    payload["username"], payload["decision"], authed.username
                )
            except AccountError as exc:
                return [
                    self._reply(
                        session, "ACCOUNT_REVIEW_RESULT", rid, {"ok": False, "error_code": exc.code}
                    )
                ]
            return [
                self._reply(
                    session,
                    "ACCOUNT_REVIEW_RESULT",
                    rid,
                    {"ok": True, "username": payload["username"], "status": status},
                )
            ]

        if kind == "LOGOUT":
            token_hash = self._session_tokens.get(session.session_id)
            if token_hash is not None and self.tokens is not None:
                self.tokens.revoke_token_hash(token_hash)
            self._close_after.add(session.session_id)
            return self.close_session(session.session_id)

        if kind == "PING":
            if authed is None:
                return [self._error(session, rid, "NOT_AUTHENTICATED")]
            return [self._reply(session, "PONG", rid, {"nonce": payload["nonce"]})]

        if kind in ("CHAT_GLOBAL", "CHAT_PRIVATE"):
            if authed is None:
                return [self._error(session, rid, "NOT_AUTHENTICATED")]
            if kind == "CHAT_GLOBAL":
                targets = [
                    s
                    for s in self._sessions.values()
                    if s.username is not None and s.session_id != session.session_id
                ]
                events = [
                    OutboundEvent(
                        t,
                        Envelope(
                            "CHAT_GLOBAL",
                            uuid.uuid4().hex,
                            {"from": authed.username, "text": payload["text"]},
                        ),
                    )
                    for t in targets
                ]
            else:
                target_id = self._by_username.get(payload["to"])
                if target_id is None:
                    if self._store_offline(authed.username, payload):
                        return [self._reply(session, "CHAT_ACK", rid, {"status": "stored"})]
                    return [self._error(session, rid, "USER_OFFLINE")]
                target = self._sessions[target_id]
                events = [
                    OutboundEvent(
                        target,
                        Envelope(
                            "CHAT_PRIVATE",
                            uuid.uuid4().hex,
                            {"from": authed.username, "text": payload["text"]},
                        ),
                    )
                ]
            return events + [self._reply(session, "CHAT_ACK", rid, {"status": "queued"})]

        if kind.startswith("FILE_"):
            if authed is None:
                return [self._error(session, rid, "NOT_AUTHENTICATED")]
            if kind == "FILE_OFFER":
                target_id = self._by_username.get(payload["to"])
                if target_id is None:
                    return [self._error(session, rid, "USER_OFFLINE")]
                outcome = self.transfers.offer(authed, self._sessions[target_id], message)
            else:
                outcome = self.transfers.handle(authed, message)
            return self.transfer_events(outcome, authed, rid)

        return [self._error(session, rid, "INVALID_MESSAGE")]
