"""Remember-me tokens: random 256-bit values, 30-day expiry, revocable.

The server stores only SHA-256(token) -- never the raw token. The raw
token is returned exactly once, at issue time, and lives only on the
client machine (see lancollab/client/token_store.py). A token is NOT
derived from the password: stealing it does not reveal the password,
and revoking it does not require changing the password.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_TTL_DAYS = 30


class TokenService:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY,
                    token_hash BLOB UNIQUE NOT NULL,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    revoked_at TEXT
                )"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash)")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _hash(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def issue(self, username: str) -> str:
        """Create a token for ``username``; return the raw token once."""
        raw = secrets.token_urlsafe(32)  # 256-bit random, not derived from anything
        now = self._now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tokens (token_hash, username, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    self._hash(raw),
                    username,
                    now.isoformat(),
                    (now + timedelta(days=TOKEN_TTL_DAYS)).isoformat(),
                ),
            )
        return raw

    def verify(self, token: str) -> tuple[str | None, str | None]:
        """Return (username, None) on success, else (None, error_code).

        error_code is TOKEN_EXPIRED, TOKEN_REVOKED, or AUTH_FAILED
        (unknown token -- deliberately the same as a wrong password).
        """
        if not isinstance(token, str) or not token:
            return None, "AUTH_FAILED"
        with self._lock:
            row = self._conn.execute(
                "SELECT username, expires_at, revoked FROM tokens WHERE token_hash = ?",
                (self._hash(token),),
            ).fetchone()
        if row is None:
            return None, "AUTH_FAILED"
        if row["revoked"]:
            return None, "TOKEN_REVOKED"
        if datetime.fromisoformat(row["expires_at"]) <= self._now():
            return None, "TOKEN_EXPIRED"
        return row["username"], None

    def revoke_token(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tokens SET revoked = 1, revoked_at = ? WHERE token_hash = ?",
                (self._now().isoformat(), self._hash(token)),
            )

    def revoke_token_hash(self, token_hash: bytes) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tokens SET revoked = 1, revoked_at = ? WHERE token_hash = ?",
                (self._now().isoformat(), token_hash),
            )

    def revoke_for_user(self, username: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tokens SET revoked = 1, revoked_at = ? WHERE username = ? AND revoked = 0",
                (self._now().isoformat(), username),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
