"""SQLite account store: scrypt password hashes, roles, pending/active status."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from lancollab.common.message_schema import FIELD_LIMITS

_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1
_MIN_PASSWORD_LEN = 8


class AccountError(Exception):
    """Registration / review failure; ``code`` is the wire error_code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AccountService:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash BLOB NOT NULL,
                    salt BLOB NOT NULL,
                    scrypt_n INTEGER NOT NULL,
                    scrypt_r INTEGER NOT NULL,
                    scrypt_p INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('member', 'admin')),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'rejected')),
                    created_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT
                )"""
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS chat_groups "
                "(name TEXT PRIMARY KEY, members TEXT NOT NULL, owner TEXT)"
            )
            if "owner" not in {row[1] for row in self._conn.execute("PRAGMA table_info(chat_groups)")}:
                # Old groups never recorded their creator; do not guess an owner.
                self._conn.execute("ALTER TABLE chat_groups ADD COLUMN owner TEXT")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _hash(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )

    def create_account(
        self,
        username: str,
        password: str,
        *,
        role: str = "member",
        status: str = "pending",
        bootstrap: bool = False,
    ) -> None:
        if not isinstance(username, str) or _USERNAME_RE.fullmatch(username) is None:
            raise AccountError("INVALID_USERNAME")
        if not isinstance(password, str) or len(password) < _MIN_PASSWORD_LEN:
            raise AccountError("INVALID_PASSWORD")
        if role not in ("member", "admin") or status not in ("pending", "active", "rejected"):
            raise AccountError("INVALID_MESSAGE")
        salt = os.urandom(16)
        digest = self._hash(password, salt)
        with self._lock, self._conn:
            # Serialize the first-user decision across separate DB connections too.
            self._conn.execute("BEGIN IMMEDIATE")
            if bootstrap:
                role = (
                    "admin"
                    if self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
                    else "member"
                )
                status = "active"
            try:
                self._conn.execute(
                    "INSERT INTO accounts (username, password_hash, salt, scrypt_n,"
                    " scrypt_r, scrypt_p, role, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        username,
                        digest,
                        salt,
                        _SCRYPT_N,
                        _SCRYPT_R,
                        _SCRYPT_P,
                        role,
                        status,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("USERNAME_TAKEN") from exc

    def _row(self, username: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM accounts WHERE username = ?", (username,)
            ).fetchone()

    def get_account(self, username: str) -> dict | None:
        """Return {username, role, status} or None. No password data."""
        row = self._row(username)
        if row is None:
            return None
        return {"username": row["username"], "role": row["role"], "status": row["status"]}

    def _revoke_tokens(self, username: str) -> None:
        """Best-effort: tokens live in the same DB file; the table may not
        exist yet if TokenService has never run."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE tokens SET revoked = 1, revoked_at = ? WHERE username = ?",
                    (self._now(), username),
                )
        except sqlite3.OperationalError:
            pass

    def authenticate(
        self, username: str, password: str, *, auto_approve=False
    ) -> tuple[bool, str | None, str | None]:
        """Return (ok, error_code, role). Password is checked BEFORE status,
        so a wrong password always returns AUTH_FAILED and never reveals
        whether the account exists or is pending/rejected."""
        row = self._row(username)
        if row is None or not isinstance(password, str):
            return False, "AUTH_FAILED", None
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=row["salt"],
            n=row["scrypt_n"],
            r=row["scrypt_r"],
            p=row["scrypt_p"],
            dklen=32,
        )
        if not hmac.compare_digest(digest, row["password_hash"]):
            return False, "AUTH_FAILED", None
        if row["status"] == "pending":
            if not auto_approve:
                return False, "ACCOUNT_PENDING", None
            with self._lock, self._conn:
                updated = self._conn.execute(
                    "UPDATE accounts SET status='active', reviewed_by='auto-approve', reviewed_at=? WHERE username=? AND status='pending'",
                    (self._now(), username),
                )
                if updated.rowcount != 1:
                    return False, "AUTH_FAILED", None
        if row["status"] == "rejected":
            return False, "ACCOUNT_REJECTED", None
        return True, None, row["role"]

    def list_pending(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, created_at FROM accounts"
                " WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [{"username": r["username"], "created_at": r["created_at"]} for r in rows]

    def review(self, username: str, decision: str, reviewer: str) -> str:
        """Approve/reject a pending account. Returns the new status.
        Raises AccountError('ACCOUNT_NOT_PENDING') if it is not pending."""
        if decision not in ("approve", "reject"):
            raise AccountError("INVALID_MESSAGE")
        status = "active" if decision == "approve" else "rejected"
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE accounts SET status = ?, reviewed_by = ?, reviewed_at = ?"
                " WHERE username = ? AND status = 'pending'",
                (status, reviewer, self._now(), username),
            )
            if cur.rowcount != 1:
                raise AccountError("ACCOUNT_NOT_PENDING")
        if status == "rejected":
            self._revoke_tokens(username)
        return status

    def change_password(self, username: str, new_password: str) -> None:
        """Server-side password reset: new salt + new scrypt hash.

        No old password is needed -- this runs on the server machine, by
        the operator who owns the database file. Raises
        AccountError('USER_NOT_FOUND') or AccountError('INVALID_PASSWORD').
        """
        if not isinstance(new_password, str) or len(new_password) < _MIN_PASSWORD_LEN:
            raise AccountError("INVALID_PASSWORD")
        salt = os.urandom(16)
        digest = self._hash(new_password, salt)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE accounts SET password_hash = ?, salt = ?,"
                " scrypt_n = ?, scrypt_r = ?, scrypt_p = ? WHERE username = ?",
                (digest, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, username),
            )
            if cur.rowcount != 1:
                raise AccountError("USER_NOT_FOUND")
        self._revoke_tokens(username)

    def create_group(self, name: str, members: list[str], creator: str) -> None:
        if (
            not isinstance(name, str) or _USERNAME_RE.fullmatch(name) is None
            or not isinstance(members, list)
            or not 2 <= len(members) <= FIELD_LIMITS["max_group_members"]
            or any(not isinstance(u, str) or _USERNAME_RE.fullmatch(u) is None for u in members)
            or len(set(members)) != len(members) or creator not in members
        ):
            raise AccountError("INVALID_MESSAGE")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._conn.execute("SELECT 1 FROM chat_groups WHERE name=?", (name,)).fetchone():
                raise AccountError("GROUP_EXISTS")
            if self._conn.execute("SELECT COUNT(*) FROM chat_groups").fetchone()[0] >= FIELD_LIMITS["max_groups"]:
                raise AccountError("GROUP_LIMIT_REACHED")
            for member in members:
                row = self._conn.execute(
                    "SELECT status FROM accounts WHERE username=?", (member,)
                ).fetchone()
                if row is None or row["status"] != "active":
                    raise AccountError("INVALID_GROUP_MEMBER")
            self._conn.execute(
                "INSERT INTO chat_groups(name, members, owner) VALUES (?, ?, ?)",
                (name, json.dumps(sorted(members)), creator),
            )

    def list_groups(self, username: str) -> list[dict]:
        # ponytail: at most 128 groups; use a membership table if that ceiling grows.
        with self._lock:
            rows = self._conn.execute("SELECT name, members, owner FROM chat_groups ORDER BY name").fetchall()
        groups = [
            {"name": row["name"], "members": json.loads(row["members"]),
             **({"owner": row["owner"]} if row["owner"] is not None else {})}
            for row in rows
        ]
        return [group for group in groups if username in group["members"]]

    def manage_group(self, name: str, actor: str, action: str, username: str | None = None) -> set[str]:
        if (
            not isinstance(action, str) or action not in {"add", "kick", "delete", "leave"}
            or not isinstance(name, str) or _USERNAME_RE.fullmatch(name) is None
            or ((username is not None) != (action in {"add", "kick"}))
            or (username is not None and (not isinstance(username, str) or _USERNAME_RE.fullmatch(username) is None))
        ):
            raise AccountError("INVALID_MESSAGE")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT members, owner FROM chat_groups WHERE name=?", (name,)).fetchone()
            if row is None or actor not in (members := json.loads(row["members"])):
                raise AccountError("GROUP_NOT_FOUND")
            owner = row["owner"]
            if owner is None:
                raise AccountError("GROUP_OWNER_UNKNOWN")
            if action != "leave" and actor != owner:
                raise AccountError("NOT_AUTHORIZED")
            affected = set(members)
            if action == "delete":
                self._conn.execute("DELETE FROM chat_groups WHERE name=?", (name,))
                return affected
            if action == "add":
                account = self._conn.execute("SELECT status FROM accounts WHERE username=?", (username,)).fetchone()
                if account is None or account["status"] != "active" or username in members:
                    raise AccountError("INVALID_GROUP_MEMBER")
                if len(members) >= FIELD_LIMITS["max_group_members"]:
                    raise AccountError("GROUP_LIMIT_REACHED")
                members.append(username)
                affected.add(username)
            else:
                target = actor if action == "leave" else username
                if target == owner:
                    raise AccountError("GROUP_OWNER_CANNOT_LEAVE")
                if target not in members:
                    raise AccountError("INVALID_GROUP_MEMBER")
                members.remove(target)
            self._conn.execute(
                "UPDATE chat_groups SET members=? WHERE name=?", (json.dumps(sorted(members)), name)
            )
            return affected

    def recover_group_owner(self, name: str, username: str) -> None:
        """Local operator recovery only: old versions never stored the creator."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT owner, members FROM chat_groups WHERE name=?", (name,)).fetchone()
            if row is None:
                raise AccountError("GROUP_NOT_FOUND")
            if row["owner"] is not None:
                raise AccountError("GROUP_OWNER_ALREADY_SET")
            account = self._conn.execute("SELECT status FROM accounts WHERE username=?", (username,)).fetchone()
            if username not in json.loads(row["members"]) or account is None or account["status"] != "active":
                raise AccountError("INVALID_GROUP_MEMBER")
            self._conn.execute("UPDATE chat_groups SET owner=? WHERE name=?", (username, name))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
