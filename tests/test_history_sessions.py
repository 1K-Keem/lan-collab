"""Login sessions persist complete conversations and are browsed without resuming."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from lancollab.client.application import CollabClient
from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.client.history import HistoryStore
from lancollab.common.contracts import Envelope
from tests.fake_transport import FakeTransport


def _authenticated_client(store, username="alice"):
    a, b = FakeTransport.pair()
    client = CollabClient(a, auth_allowed=True, history=store, history_server="server")
    rid = client.login(username, "synthetic-password")
    b.receive()
    client.handle_event(
        Envelope("AUTH_RESULT", rid, {"ok": True, "username": username, "role": "member"})
    )
    return a, b, client


class SessionStoreTests(unittest.TestCase):
    def test_recent_filters_by_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append("server", "alice", "sent", "CHAT_PRIVATE", "bob", "old", session="s1")
            store.append("server", "alice", "sent", "CHAT_PRIVATE", "bob", "new", session="s2")
            self.assertEqual(
                [row[-1] for row in store.recent("server", "alice", session="s1")], ["old"]
            )
            self.assertEqual(
                [row[-1] for row in store.recent("server", "alice", session="s2")], ["new"]
            )
            self.assertEqual([row[-1] for row in store.recent("server", "alice")], ["old", "new"])

    def test_sessions_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append("server", "alice", "sent", "CHAT_PRIVATE", "bob", "one", session="s1")
            store.append("server", "alice", "sent", "CHAT_PRIVATE", "bob", "two", session="s1")
            store.append("server", "alice", "sent", "CHAT_PRIVATE", "bob", "three", session="s2")
            listed = store.sessions("server", "alice")
            self.assertEqual([s[0] for s in listed], ["s1", "s2"])
            self.assertEqual(listed[0][3], 2)
            self.assertEqual(listed[1][3], 1)

    def test_legacy_database_without_session_column_migrates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY, server TEXT NOT NULL, account TEXT NOT NULL,
                timestamp TEXT NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL,
                peer TEXT NOT NULL, text TEXT NOT NULL)""")
            legacy.execute(
                "INSERT INTO messages(server, account, timestamp, direction, kind, peer, text)"
                " VALUES('server','alice','2026-11-10T10:00:00+00:00','received','CHAT_PRIVATE','bob','legacy')"
            )
            legacy.commit()
            legacy.close()
            store = HistoryStore(path)
            self.addCleanup(store.close)
            self.assertEqual([row[-1] for row in store.recent("server", "alice")], ["legacy"])
            self.assertEqual([s[0] for s in store.sessions("server", "alice")], [""])
            self.assertEqual(store.session_records("server", "alice")[0].count, 1)
            self.assertEqual(store.transcript("server", "alice", "")[0][-1], "legacy")
            store.append("server", "alice", "sent", "CHAT_PRIVATE", "bob", "new", session="s9")
            self.assertEqual(
                [row[0] for row in store.recent("server", "alice", with_session=True)],
                ["", "s9"],
            )


class SessionCommandTests(unittest.TestCase):
    def test_client_records_session_id_and_clears_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            a, b, client = _authenticated_client(store)
            self.addCleanup(client.close)
            self.assertEqual(len(client.session_id or ""), 12)
            rid = client.send_private("bob", "hello")
            b.receive()
            client.handle_event(Envelope("CHAT_ACK", rid, {"status": "queued"}))
            rows = store.recent("server", "alice", with_session=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], client.session_id)
            client.close()
            self.assertIsNone(client.session_id)

    def test_history_lists_current_and_older_sessions_without_opening_them(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append(
                "server", "alice", "received", "CHAT_PRIVATE", "bob", "old msg", session="oldsess"
            )
            a, b, client = _authenticated_client(store)
            self.addCleanup(client.close)
            output = []
            shell = ConsoleSession(a, history=store, history_server="server", output=output.append)
            shell.client = client
            shell.execute(parse_command("/history"))
            self.assertTrue(any("oldsess" in line for line in output))
            self.assertTrue(any("current" in line for line in output))
            self.assertTrue(any("/history show ID" in line for line in output))
            self.assertFalse(any("old msg" in line for line in output))

    def test_full_session_survives_quit_and_logout_with_all_peers(self):
        for command in ("/quit", "/logout"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.sqlite3"
                store = HistoryStore(path)
                login_time = datetime(2026, 11, 11, 3, 24, tzinfo=timezone.utc)
                with patch("lancollab.client.history.datetime") as clock:
                    clock.now.return_value = login_time
                    a, b, client = _authenticated_client(store)
                session_id = client.session_id
                for index in range(60):
                    peer = "bob" if index % 2 else "carol"
                    client.handle_event(
                        Envelope(
                            "CHAT_PRIVATE",
                            f"dm-{index}",
                            {"from": peer, "text": f"private message {index}"},
                        )
                    )
                client.handle_event(
                    Envelope("CHAT_GLOBAL", "public", {"from": "dave", "text": "global message"})
                )
                shell = ConsoleSession(a, output=lambda _: None)
                shell.client = client
                self.assertFalse(shell.execute(parse_command(command)))
                record = store.session_records("server", "alice")[0]
                self.assertEqual(record.started, login_time.isoformat(timespec="seconds"))
                self.assertIsNotNone(record.ended)
                store.close()

                store = HistoryStore(path)
                self.addCleanup(store.close)
                a, b, client = _authenticated_client(store)
                self.addCleanup(client.close)
                self.assertNotEqual(client.session_id, session_id)
                store.append(
                    "server",
                    "eve",
                    "received",
                    "CHAT_PRIVATE",
                    "bob",
                    "other account secret",
                    session=session_id,
                )
                store.append(
                    "other-server",
                    "alice",
                    "received",
                    "CHAT_PRIVATE",
                    "bob",
                    "other server secret",
                    session=session_id,
                )
                output = []
                shell = ConsoleSession(a, output=output.append)
                shell.client = client
                client.drain_notices()
                before = client.session_id
                shell.execute(parse_command(f"/history show {session_id}"))
                joined = "\n".join(output)
                self.assertIn("private message 0", joined)
                self.assertIn("private message 59", joined)
                self.assertIn("global message", joined)
                self.assertIn("bob:", joined)
                self.assertIn("carol:", joined)
                self.assertNotIn("secret", joined)
                self.assertEqual(client.session_id, before)
                self.assertEqual(len(store.transcript("server", "alice", session_id)), 61)
                with self.assertRaises(TimeoutError):
                    b.receive()

    def test_session_id_from_another_account_cannot_be_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append(
                "server", "eve", "received", "CHAT_PRIVATE", "bob", "secret", session="hidden"
            )
            a, b, client = _authenticated_client(store)
            self.addCleanup(client.close)
            shell = ConsoleSession(a, output=lambda _: None)
            shell.client = client
            with self.assertRaisesRegex(ValueError, "not found"):
                shell.history_transcript("hidden")

    def test_empty_session_and_token_login_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            a, b = FakeTransport.pair()
            client = CollabClient(a, auth_allowed=True, history=store, history_server="server")
            self.addCleanup(client.close)
            rid = client.login_with_token("synthetic-token")
            b.receive()
            client.handle_event(
                Envelope("AUTH_RESULT", rid, {"ok": True, "username": "alice", "role": "member"})
            )
            self.assertEqual(store.session_records("server", "alice")[0].count, 0)
            client.close()
            self.assertIsNotNone(store.session_records("server", "alice")[0].ended)

    def test_tui_picker_opens_old_session_and_ignores_typing(self):
        try:
            import curses
        except ImportError:
            self.skipTest("curses is not installed")
        from lancollab.client.tui import run_tui

        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append(
                "server",
                "alice",
                "received",
                "CHAT_PRIVATE",
                "bob",
                "old private message",
                session="old",
            )
            store.append(
                "server",
                "alice",
                "received",
                "CHAT_GLOBAL",
                "carol",
                "old global message",
                session="old",
            )
            a, b, client = _authenticated_client(store)
            shell = ConsoleSession(a, output=lambda _: None)
            shell.client = client
            window = Mock()
            window.getmaxyx.return_value = (24, 100)
            window.get_wch.side_effect = (
                list("/history\n")
                + [curses.KEY_DOWN, "\n"]
                + list("must never send\n")
                + [curses.KEY_NPAGE, KeyboardInterrupt, "\x1b"]
                + list("/quit\n")
            )
            with patch("curses.wrapper", side_effect=lambda fn: fn(window)):
                run_tui(shell)
            drawn = " ".join(call.args[2] for call in window.addstr.call_args_list)
            self.assertIn("Session history · read only", drawn)
            self.assertIn("old private message", drawn)
            self.assertIn("old global message", drawn)
            with self.assertRaises(TimeoutError):
                b.receive()

    def test_history_all_groups_sessions_with_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append(
                "server", "alice", "received", "CHAT_PRIVATE", "bob", "old msg", session="oldsess"
            )
            a, b, client = _authenticated_client(store)
            self.addCleanup(client.close)
            rid = client.send_private("bob", "new msg")
            b.receive()
            client.handle_event(Envelope("CHAT_ACK", rid, {"status": "queued"}))
            client.drain_notices()  # Echo notices must not leak into /history output.
            output = []
            shell = ConsoleSession(a, history=store, history_server="server", output=output.append)
            shell.client = client
            shell.execute(parse_command("/history all"))
            self.assertTrue(any("old msg" in line for line in output))
            self.assertTrue(any("new msg" in line for line in output))
            headers = [line for line in output if line.startswith("── Session ")]
            self.assertEqual(len(headers), 2)
            # Current session is shown after the older one.
            self.assertLess(
                next(i for i, line in enumerate(output) if "old msg" in line),
                next(i for i, line in enumerate(output) if "new msg" in line),
            )

    def test_history_peer_filter_stays_in_current_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            store.append(
                "server", "alice", "received", "CHAT_PRIVATE", "bob", "old bob", session="oldsess"
            )
            a, b, client = _authenticated_client(store)
            self.addCleanup(client.close)
            rid = client.send_private("bob", "new bob")
            b.receive()
            client.handle_event(Envelope("CHAT_ACK", rid, {"status": "queued"}))
            rid = client.send_private("carol", "new carol")
            b.receive()
            client.handle_event(Envelope("CHAT_ACK", rid, {"status": "queued"}))
            client.drain_notices()  # Echo notices must not leak into /history output.
            output = []
            shell = ConsoleSession(a, history=store, history_server="server", output=output.append)
            shell.client = client
            shell.execute(parse_command("/history bob"))
            self.assertTrue(any("new bob" in line for line in output))
            self.assertFalse(any("old bob" in line for line in output))
            self.assertFalse(any("new carol" in line for line in output))


if __name__ == "__main__":
    unittest.main()
