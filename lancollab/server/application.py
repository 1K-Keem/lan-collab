"""TV1 server handler: auth, sessions, presence, chat routing.

No networking here. handle_message() returns OutboundEvents; the
connection layer in connections.py delivers them to the right transports.
"""

from __future__ import annotations

import uuid
from lancollab.common.contracts import Envelope, OutboundEvent, SessionIdentity
from lancollab.server.accounts import AccountError, AccountService

MAX_FAILED_ATTEMPTS = 3


class CollabServer:
    def __init__(self, accounts: AccountService) -> None:
        self.accounts = accounts
        self._sessions: dict[str, SessionIdentity] = {}  # session_id -> identity
        self._by_username: dict[str, str] = {}  # username -> session_id
        self._failed: dict[str, int] = {}  # session_id -> failed auth attempts
        self._close_after: set[str] = set()  # session_ids to close after delivery

    # -- helpers ---------------------------------------------------------
    def session_for(self, session_id: str) -> SessionIdentity | None:
        return self._sessions.get(session_id)

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

    # -- lifecycle -------------------------------------------------------
    def close_session(self, session_id: str) -> list[OutboundEvent]:
        ident = self._sessions.pop(session_id, None)
        self._failed.pop(session_id, None)
        if ident is None or ident.username is None:
            return []
        self._by_username.pop(ident.username, None)
        return self._presence(ident, "offline", exclude=session_id)

    # -- dispatch --------------------------------------------------------
    def handle_message(self, message: Envelope, session: SessionIdentity) -> list[OutboundEvent]:
        kind, payload, rid = message.type, message.payload, message.request_id
        authed = self._sessions.get(session.session_id)

        if kind == "REGISTER_REQUEST":
            if authed is not None:
                return [self._error(session, rid, "INVALID_STATE")]
            try:
                self.accounts.create_account(payload["username"], payload["password"])
            except AccountError as exc:
                return [
                    self._reply(
                        session, "REGISTER_RESULT", rid, {"ok": False, "error_code": exc.code}
                    )
                ]
            self._close_after.add(session.session_id)  # close after delivery; log in later
            return [self._reply(session, "REGISTER_RESULT", rid, {"ok": True, "status": "pending"})]

        if kind == "AUTH_REQUEST":
            if authed is not None:
                return [self._error(session, rid, "INVALID_STATE")]
            ok, error_code, role = self.accounts.authenticate(
                payload["username"], payload["password"]
            )
            if not ok:
                self._failed[session.session_id] = self._failed.get(session.session_id, 0) + 1
                if self._failed[session.session_id] >= MAX_FAILED_ATTEMPTS:
                    self._close_after.add(session.session_id)
                return [
                    self._reply(
                        session, "AUTH_RESULT", rid, {"ok": False, "error_code": error_code}
                    )
                ]
            self._failed.pop(session.session_id, None)
            closed = self._takeover(payload["username"], session.session_id)
            self._close_after.update(closed)
            ident = SessionIdentity(
                session.remote_ip,
                session.remote_port,
                session.session_id,
                payload["username"],
                role,
            )
            self._sessions[ident.session_id] = ident
            self._by_username[ident.username] = ident.session_id
            events = [
                self._reply(
                    ident,
                    "AUTH_RESULT",
                    rid,
                    {"ok": True, "username": ident.username, "role": role},
                ),
                self._user_list(ident),
            ]
            return events + self._presence(ident, "online", exclude=ident.session_id)

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
            # File relay via TransferRegistry is wave 3, not this commit.
            return [self._error(session, rid, "NOT_IMPLEMENTED")]

        return [self._error(session, rid, "INVALID_MESSAGE")]
