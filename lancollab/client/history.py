"""Local chat archive, isolated by trusted server identity and logged-in account."""

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from threading import Lock
from dataclasses import dataclass


@dataclass(frozen=True)
class HistorySession:
    id: str
    started: str
    ended: str | None
    count: int


def local_time(timestamp):
    """Display stored UTC timestamps in the viewer's local timezone."""
    return datetime.fromisoformat(timestamp).astimezone().strftime("%d/%m/%Y %H:%M:%S %Z")


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
        self._migrate()
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS history_account ON messages(server, account, id)"
        )
        self._db.commit()

    def _migrate(self):
        """Add the session column to archives created before sessions existed."""
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(messages)")]
        if "session" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN session TEXT NOT NULL DEFAULT ''")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS history_session ON messages(server, account, session, id)"
        )
        self._db.execute("""CREATE TABLE IF NOT EXISTS history_sessions (
            server TEXT NOT NULL, account TEXT NOT NULL, session TEXT NOT NULL,
            started TEXT NOT NULL, ended TEXT,
            PRIMARY KEY (server, account, session))""")
        self._db.execute(
            "INSERT OR IGNORE INTO history_sessions(server, account, session, started) "
            "SELECT server, account, session, MIN(timestamp) FROM messages "
            "GROUP BY server, account, session"
        )

    def start_session(self, server, account, session):
        with self._lock:
            self._db.execute(
                "INSERT INTO history_sessions(server, account, session, started) VALUES(?,?,?,?)",
                (
                    server,
                    account,
                    session,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            self._db.commit()

    def end_session(self, server, account, session):
        with self._lock:
            self._db.execute(
                "UPDATE history_sessions SET ended=? "
                "WHERE server=? AND account=? AND session=? AND ended IS NULL",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    server,
                    account,
                    session,
                ),
            )
            self._db.commit()

    def session_records(self, server, account):
        """Newest login first, including sessions with no messages."""
        with self._lock:
            return [
                HistorySession(*row)
                for row in self._db.execute(
                    "SELECT s.session, s.started, s.ended, COUNT(m.id) "
                    "FROM history_sessions s LEFT JOIN messages m ON "
                    "m.server=s.server AND m.account=s.account AND m.session=s.session "
                    "WHERE s.server=? AND s.account=? GROUP BY s.session "
                    "ORDER BY s.started DESC, s.rowid DESC",
                    (server, account),
                ).fetchall()
            ]

    def transcript(self, server, account, session):
        """All messages from exactly one account/server/session, oldest first."""
        with self._lock:
            return self._db.execute(
                "SELECT timestamp, direction, kind, peer, text FROM messages "
                "WHERE server=? AND account=? AND session=? ORDER BY id",
                (server, account, session),
            ).fetchall()

    def direct_conversations(self, server, account):
        """Private conversations only: (peer, last_timestamp, last_text).

        Newest last message first, across every saved login session.
        """
        with self._lock:
            return self._db.execute(
                "SELECT m.peer, m.timestamp, m.text FROM messages m JOIN ("
                "SELECT peer, MAX(id) AS id FROM messages "
                "WHERE server=? AND account=? AND kind='CHAT_PRIVATE' AND peer<>? "
                "GROUP BY peer) last ON m.id=last.id ORDER BY m.id DESC",
                (server, account, account),
            ).fetchall()

    def direct_transcript(self, server, account, peer):
        """Every private message with ``peer``, oldest first, all sessions.

        Unlike :meth:`recent` this has no row limit; the complete conversation
        is returned for the Direct tab's peer page.
        """
        with self._lock:
            return self._db.execute(
                "SELECT timestamp, direction, kind, peer, text FROM messages "
                "WHERE server=? AND account=? AND kind='CHAT_PRIVATE' AND peer=? "
                "ORDER BY id",
                (server, account, peer),
            ).fetchall()

    def append(self, server, account, direction, kind, peer, text, session=""):
        """Save one message. ``session`` is the login session that produced it.

        A new session id is issued on every successful login, so quitting
        and coming back starts a new session instead of extending the old one.
        Rows saved before sessions existed carry session "".
        """
        with self._lock:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._db.execute(
                "INSERT OR IGNORE INTO history_sessions(server, account, session, started) "
                "VALUES(?,?,?,?)",
                (server, account, session, timestamp),
            )
            self._db.execute(
                "INSERT INTO messages(server, account, timestamp, direction, kind, peer, text, session)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    server,
                    account,
                    timestamp,
                    direction,
                    kind,
                    peer,
                    text,
                    session,
                ),
            )
            self._db.commit()

    def recent(self, server, account, peer=None, limit=50, session=None, with_session=False):
        """Last ``limit`` messages, oldest first.

        ``session`` filters to one login session (None = all sessions).
        With ``with_session=True`` each row is prefixed with its session id:
        ``(session, timestamp, direction, kind, peer, text)``.
        """
        if not 1 <= limit <= 500:
            raise ValueError("History limit must be 1–500")
        columns = (
            "session, timestamp, direction, kind, peer, text"
            if with_session
            else ("timestamp, direction, kind, peer, text")
        )
        query = f"SELECT {columns} FROM messages WHERE server=? AND account=?"
        params = [server, account]
        if peer is not None:
            query += " AND peer=?"
            params.append(peer)
        if session is not None:
            query += " AND session=?"
            params.append(session)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            return list(reversed(self._db.execute(query, [*params, limit]).fetchall()))

    def search(self, server, account, text, limit=50):
        """Literal substring search across this account's sessions, newest 50 results."""
        if not text.strip() or not 1 <= limit <= 500:
            raise ValueError("Enter search text; limit must be 1–500")
        with self._lock:
            return list(
                reversed(
                    self._db.execute(
                        "SELECT timestamp, direction, kind, peer, text FROM messages "
                        "WHERE server=? AND account=? AND "
                        "(instr(lower(text), lower(?)) > 0 OR instr(lower(peer), lower(?)) > 0) "
                        "ORDER BY id DESC LIMIT ?",
                        (server, account, text, text, limit),
                    ).fetchall()
                )
            )

    def sessions(self, server, account):
        """Login sessions that have saved messages: (id, first, last, count).

        Ordered oldest first. The "" session holds rows saved before
        sessions existed.
        """
        with self._lock:
            return self._db.execute(
                "SELECT session, MIN(timestamp), MAX(timestamp), COUNT(*)"
                " FROM messages WHERE server=? AND account=?"
                " GROUP BY session ORDER BY MIN(timestamp) ASC",
                (server, account),
            ).fetchall()

    def close(self):
        with self._lock:
            self._db.close()
