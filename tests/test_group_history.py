"""Run with python -m unittest tests.test_group_history."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

from lancollab.client.application import ClientState
from lancollab.client.cli import ConsoleSession
from lancollab.client.history import HistoryStore
from lancollab.common.contracts import Envelope
from tests.fake_transport import FakeTransport
from tests.test_tui_context_menu import click, expect, run


class GroupHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.db"
        self.store = HistoryStore(self.path)
        self.addCleanup(self.store.close)
        transport, self.peer = FakeTransport.pair()
        self.shell = ConsoleSession(
            transport, output=lambda _: None, downloads_dir=self.tmp.name,
            history=self.store, history_server="server-a",
        )
        self.addCleanup(self.shell.close)
        self.client = self.shell.client
        self.client.state, self.client.username, self.client.session_id = ClientState.AUTHENTICATED, "alice", "current"
        self.client.groups = {"study": ["alice", "bob"]}

    def save_old(self, text, **scope):
        self.store.append(
            scope.get("server", "server-a"), scope.get("account", "alice"),
            "received", scope.get("kind", "CHAT_GROUP"), scope.get("peer", "#study"),
            f"bob: {text}", session=scope.get("session", "old-login"),
        )

    def receive(self, text):
        self.client.handle_event(Envelope("CHAT_GROUP", f"event-{self.client.group_revision}", {
            "group": "study", "from": "bob", "text": text,
        }))
        self.shell._drain()

    def test_full_transcript_survives_reopen_and_isolates_accounts_servers_groups(self):
        for number in range(80):
            self.save_old(f"old {number}", session=f"old-{number // 40}")
        self.save_old("other group", peer="#other")
        self.save_old("other server", server="server-b")
        self.save_old("other account", account="carol")
        self.save_old("private excluded", kind="CHAT_PRIVATE")
        reopened = HistoryStore(self.path)
        try:
            self.client.history = reopened
            rows = self.shell.group_transcript("study")
            self.assertEqual(len(rows), 80)
            self.assertEqual([rows[0][-1], rows[-1][-1]], ["bob: old 0", "bob: old 79"])
            self.assertTrue(all(row[2:4] == ("CHAT_GROUP", "#study") for row in rows))
            self.client.username = "carol"
            self.assertEqual(self.shell.group_transcript("study")[0][-1], "bob: other account")
            self.client.username = "alice"
            self.client.history_server = "server-b"
            self.assertEqual(self.shell.group_transcript("study")[0][-1], "bob: other server")
        finally:
            self.client.history = self.store
            reopened.close()

    def test_only_acknowledged_outgoing_and_received_messages_are_saved(self):
        request_id = self.client.send_group("study", "sent")
        self.peer.receive()
        self.assertEqual(self.shell.group_transcript("study"), [])
        self.client.handle_event(Envelope("CHAT_ACK", request_id, {"status": "queued"}))
        self.receive("received")
        self.assertEqual([row[-1] for row in self.shell.group_transcript("study")], ["sent", "bob: received"])

    def test_no_history_keeps_full_current_session_but_writes_nothing(self):
        self.client.history = None
        for number in range(2100):
            self.client._remember_chat("received", "CHAT_GROUP", "#study", f"bob: live {number}")
        rows = self.shell.group_transcript("study")
        self.assertEqual(len(rows), 2100)
        self.assertEqual(rows[0][-1], "bob: live 0")
        self.assertEqual(self.store.archive("server-a", "alice"), [])

    def test_write_failure_preserves_live_message_in_order_without_duplicates(self):
        self.save_old("old")
        with patch.object(self.store, "append", side_effect=sqlite3.OperationalError("disk full")):
            self.receive("unsaved")
        self.receive("saved next")
        rows = self.shell.group_transcript("study")
        self.assertEqual([row[-1] for row in rows], ["bob: old", "bob: unsaved", "bob: saved next"])
        self.assertEqual(len(self.store.archive("server-a", "alice")), 2)

    def test_delete_invalidates_cache_and_removes_unsaved_fallback(self):
        self.receive("saved")
        with patch.object(self.store, "append", side_effect=sqlite3.OperationalError("disk full")):
            self.receive("unsaved")
        revision = self.client.group_revision
        self.assertEqual(self.shell.delete_history(peer="#study"), 1)
        self.assertGreater(self.client.group_revision, revision)
        self.assertEqual(self.shell.group_transcript("study"), [])

    def test_failed_append_rolls_back_its_session_record(self):
        with self.store._db:
            self.store._db.execute(
                "CREATE TRIGGER fail_message BEFORE INSERT ON messages "
                "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append("server-a", "alice", "sent", "CHAT_GROUP", "#study", "no", session="failed")
        self.assertEqual(self.store.session_records("server-a", "alice"), [])

    def test_tui_loads_saved_messages_and_refreshes_only_on_group_changes(self):
        self.save_old("archived message")

        def incoming(window):
            self.receive("new message")
            return ""

        with patch.object(self.store, "archive", wraps=self.store.archive) as query:
            run(self.shell, [
                click("3:Groups"),
                expect(lambda w: "archived message" in w.all_text(), ""),
                expect(lambda w: query.call_count == 1, incoming),
                expect(lambda w: w.all_text().count("new message") == 1 and query.call_count == 2, "\x03"),
            ])

    def test_read_failure_shows_live_messages_and_an_error(self):
        with patch.object(self.store, "archive", side_effect=sqlite3.OperationalError("locked")):
            def incoming(window):
                self.receive("still visible")
                return ""

            run(self.shell, [
                click("3:Groups"), incoming,
                expect(lambda w: "still visible" in w.all_text() and "Could not read saved" in w.all_text(),
                       "\x03"),
            ])

    def test_saved_message_copy_uses_original_body(self):
        self.save_old("archived copy")
        with patch("lancollab.client.tui.copy_text") as copy:
            run(self.shell, [click("3:Groups"), lambda w: w.module.KEY_F9, "c", "\x1b", "\x03"])
        copy.assert_called_once_with("archived copy")


if __name__ == "__main__":
    unittest.main()
