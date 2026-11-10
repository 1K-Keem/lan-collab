"""Local chat archive, isolated by trusted server identity and logged-in account."""

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from threading import Lock


class HistoryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self._lock = Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, server TEXT NOT NULL, account TEXT NOT NULL,
            timestamp TEXT NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL,
            peer TEXT NOT NULL, text TEXT NOT NULL)""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS history_account ON messages(server, account, id)"
        )
        self._db.commit()

    def append(self, server, account, direction, kind, peer, text):
        with self._lock:
            self._db.execute(
                "INSERT INTO messages(server, account, timestamp, direction, kind, peer, text) VALUES(?,?,?,?,?,?,?)",
                (
                    server,
                    account,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    direction,
                    kind,
                    peer,
                    text,
                ),
            )
            self._db.commit()

    def recent(self, server, account, peer=None, limit=50):
        if not 1 <= limit <= 500:
            raise ValueError("History limit must be 1–500")
        query = "SELECT timestamp, direction, kind, peer, text FROM messages WHERE server=? AND account=?"
        params = [server, account]
        if peer is not None:
            query += " AND peer=?"
            params.append(peer)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            return list(reversed(self._db.execute(query, [*params, limit]).fetchall()))

    def close(self):
        with self._lock:
            self._db.close()
