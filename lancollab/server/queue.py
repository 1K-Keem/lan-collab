"""Offline message queue: store private chat for users who are away.

Only *text* private messages are queued. Files are never stored: a file
transfer still needs both sides online at the same time. Group chat is
not queued either — it is a live broadcast to whoever is online now.

Delivery is at-most-once: ``drain`` returns the stored messages in
``created_at`` order and deletes them. If the server crashes between
draining and delivering, those messages are lost; that is the documented
trade-off for not keeping an acked-delivery protocol.

Bounds keep one user from filling the disk with messages to an account
that never logs in: at most ``MAX_QUEUED_PER_USER`` messages per
recipient, each living at most ``QUEUE_TTL_DAYS``. When the queue is
full, the oldest message is dropped to make room (FIFO).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_QUEUED_PER_USER = 100
QUEUE_TTL_DAYS = 1


class MessageQueue:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS queued_messages (
                    id INTEGER PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queued_recipient "
                "ON queued_messages(recipient, created_at)"
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def count(self, recipient: str) -> int:
        """Number of live (unexpired) messages waiting for ``recipient``."""
        # Commit like the other methods: the prune DELETE opens a write
        # transaction, and leaving it open would hold the database lock
        # and block every other connection to this file.
        with self._lock, self._conn:
            self._prune_expired_locked(recipient)
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM queued_messages WHERE recipient = ?",
                (recipient,),
            ).fetchone()
            return int(row["n"])

    def enqueue(self, recipient: str, sender: str, kind: str, payload: dict) -> bool:
        """Store one message. Always succeeds: oldest is dropped if full."""
        now = self._now()
        with self._lock, self._conn:
            self._prune_expired_locked(recipient)
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM queued_messages WHERE recipient = ?",
                (recipient,),
            ).fetchone()
            if int(row["n"]) >= MAX_QUEUED_PER_USER:
                self._conn.execute(
                    "DELETE FROM queued_messages WHERE id IN ("
                    " SELECT id FROM queued_messages"
                    " WHERE recipient = ? ORDER BY created_at ASC, id ASC LIMIT 1)",
                    (recipient,),
                )
            self._conn.execute(
                "INSERT INTO queued_messages"
                " (recipient, sender, kind, payload, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    recipient,
                    sender,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    now.isoformat(),
                    (now + timedelta(days=QUEUE_TTL_DAYS)).isoformat(),
                ),
            )
        return True

    def drain(self, recipient: str) -> list[dict]:
        """Return waiting messages oldest-first and delete them.

        Each item is ``{"sender": ..., "kind": ..., "payload": ...}``.
        Expired messages are dropped without being returned.
        """
        with self._lock, self._conn:
            self._prune_expired_locked(recipient)
            rows = self._conn.execute(
                "SELECT id, sender, kind, payload FROM queued_messages"
                " WHERE recipient = ? ORDER BY created_at ASC, id ASC",
                (recipient,),
            ).fetchall()
            if rows:
                self._conn.execute(
                    "DELETE FROM queued_messages WHERE recipient = ?",
                    (recipient,),
                )
            return [
                {
                    "sender": row["sender"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                }
                for row in rows
            ]

    def drop_for_user(self, recipient: str) -> None:
        """Delete everything waiting for ``recipient`` (e.g. account rejected)."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM queued_messages WHERE recipient = ?",
                (recipient,),
            )

    def _prune_expired_locked(self, recipient: str | None = None) -> None:
        """Delete expired rows. Caller must hold ``self._lock``."""
        if recipient is None:
            self._conn.execute(
                "DELETE FROM queued_messages WHERE expires_at <= ?",
                (self._now().isoformat(),),
            )
        else:
            self._conn.execute(
                "DELETE FROM queued_messages WHERE recipient = ? AND expires_at <= ?",
                (recipient, self._now().isoformat()),
            )
