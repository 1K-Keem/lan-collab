"""SQLite account store: scrypt password hashes, roles, pending/active status."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

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
        self, username: str, password: str, *, role: str = "member", status: str = "pending"
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

    def authenticate(self, username: str, password: str) -> tuple[bool, str | None, str | None]:
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
            return False, "ACCOUNT_PENDING", None
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
