"""People history navigation, viewer isolation and compact chat presentation."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lancollab.client.application import ClientState, Notice
from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.client.history import HistoryStore
from lancollab.client.tui import HistoryBrowser, cell_width, message_rows, run_tui
from tests.fake_transport import FakeTransport
from tests.test_tui_context_menu import FakeWindow, click, expect, fake_curses


class HistoryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = HistoryStore(Path(self.directory.name) / "archive.sqlite3")
        self.addCleanup(self.store.close)
        left, self.remote = FakeTransport.pair()
        self.output = []
        self.session = ConsoleSession(
            left, history=self.store, history_server="server", output=self.output.append
        )
        self.session.client.username = "alice"
        self.session.client.session_id = "current"
        self.session.client.state = ClientState.AUTHENTICATED
        self.addCleanup(self.session.close)
        for number in range(60):
            self.store.append(
                "server",
                "alice",
                "received",
                "CHAT_PRIVATE",
                "bob",
                f"old message {number}",
                session="old",
            )
        self.store.append(
            "server",
            "alice",
            "received",
            "CHAT_GLOBAL",
            "carol",
            "public from carol",
            session="old",
        )
        self.store.append(
            "server", "alice", "received", "CHAT_PRIVATE", "bob", "hello now", session="current"
        )
        self.store.append(
            "server", "alice", "sent", "CHAT_PRIVATE", "bob", "my reply", session="current"
        )
        self.store.append(
            "server",
            "alice",
            "received",
            "CHAT_GLOBAL",
            "bob",
            "public from bob",
            session="current",
        )
        self.store.append(
            "server", "alice", "sent", "CHAT_GLOBAL", "*", "my public message", session="current"
        )
        for server, account in [("other-server", "alice"), ("server", "eve")]:
            self.store.append(
                server,
                account,
                "received",
                "CHAT_PRIVATE",
                "hidden-person",
                "secret",
                session="current",
            )

    def test_session_picker_opens_exact_login(self):
        browser = HistoryBrowser(self.session)
        self.assertEqual(browser.level, "sessions")
        self.assertEqual([(r.id, r.count) for r in browser.records], [("current", 4), ("old", 61)])
        browser.open()
        self.assertEqual(
            [r[-1] for r in browser.transcript],
            ["hello now", "my reply", "public from bob", "my public message"],
        )
        self.assertEqual(self.session.client.session_id, "current")
        browser.back()
        browser.move(1)
        browser.open()
        self.assertEqual(len(browser.transcript), 61)
        self.assertEqual(browser.transcript[0][-1], "old message 0")
        browser.back()
        self.assertEqual(browser.selected, 1)
        self.assertFalse(browser.back())
        with self.assertRaises(TimeoutError):
            self.remote.receive()

    def test_people_open_complete_scoped_history(self):
        browser = HistoryBrowser(self.session)
        browser.switch_list()
        self.assertEqual([row[0] for row in browser.people], ["bob", "carol"])
        browser.open()
        self.assertEqual(browser.level, "messages")
        self.assertEqual(browser.peer, "bob")
        self.assertEqual(len(browser.transcript), 63)
        self.assertEqual(browser.session_ids[:60], ["old"] * 60)
        self.assertEqual(browser.session_ids[60:], ["current"] * 3)
        browser.back()
        self.assertEqual(browser.level, "people")
        self.assertEqual(browser.selected, 0)
        self.assertNotIn("All messages", " ".join(label for label, _ in browser.items()))
        direct = HistoryBrowser(self.session, ("bob",))
        self.assertEqual(direct.transcript, browser.transcript)

    def test_resize_scroll_and_no_cross_peer_leak(self):
        browser = HistoryBrowser(self.session, ("bob",))
        for width in (76, 36, 16):
            page, _ = browser.page(width, 1000)
            self.assertTrue(all(cell_width(line) <= width for line, _ in page))
            text = "\n".join(line for line, _ in page)
            self.assertNotIn("carol", text)
            self.assertNotIn("secret", text)
            self.assertNotIn("CHAT_PRIVATE", text)
            self.assertIn("Session", text)
        browser.page(76, 8)
        browser.move(10**9)
        page, _ = browser.page(76, 8)
        self.assertIn("public from bob", "\n".join(line for line, _ in page))
        browser.move(-(10**9))
        page, _ = browser.page(76, 8)
        self.assertIn("old message 0", "\n".join(line for line, _ in page))

    def test_transcripts_open_at_latest_and_allow_scrolling_up(self):
        for args, latest in [(("bob",), "public from bob"), (("show", "old"), "public from carol")]:
            browser = HistoryBrowser(self.session, args)
            page, start = browser.page(76, 8)
            self.assertGreater(start, 0)
            self.assertIn(latest, " ".join(line for line, _ in page))
            browser.move(-3)
            _, earlier = browser.page(76, 8)
            self.assertLess(earlier, start)
            browser.move(-(10**9))
            page, start = browser.page(76, 8)
            self.assertEqual(start, 0)
            self.assertIn("old message 0", " ".join(line for line, _ in page))
            browser.back()
            if args == ("bob",):
                browser.open()
                page, _ = browser.page(76, 8)
                self.assertIn(latest, " ".join(line for line, _ in page))

    def test_plain_commands_list_people_and_show_one_peer(self):
        self.session.execute(parse_command("/history people"))
        listed = "\n".join(self.output)
        self.assertIn("bob", listed)
        self.assertIn("carol", listed)
        self.assertNotIn("hidden-person", listed)
        self.output.clear()
        self.session.execute(parse_command("/history show current bob"))
        text = "\n".join(self.output)
        self.assertIn("[DM] bob: hello now", text)
        self.assertIn("[DM] you → bob: my reply", text)
        self.assertIn("[All] bob: public from bob", text)
        self.assertNotIn("carol", text)
        self.assertNotIn("old message", text)
        self.assertNotIn("CHAT_PRIVATE", text)

    def test_full_screen_mouse_keyboard_and_read_only_input(self):
        def resize(window):
            window.columns = 40
            return window.module.KEY_RESIZE

        window = FakeWindow(
            24,
            100,
            [
                "/history\n",
                expect(lambda w: "Your login sessions" in w.all_text(), "\n"),
                expect(lambda w: "hello now" in w.all_text(), "must never send\n"),
                resize,
                expect(lambda w: "hello now" in w.all_text(), "\x1b"),
                expect(lambda w: "Your login sessions" in w.all_text(), "\t"),
                expect(lambda w: "Choose a person" in w.all_text(), click("bob ·")),
                expect(lambda w: "public from bob" in w.all_text(), lambda w: w.module.KEY_HOME),
                expect(lambda w: "old message 0" in w.all_text(), "\x1b"),
                expect(lambda w: "Choose a person" in w.all_text(), "\x1b"),
                "\x03",
            ],
        )
        module = fake_curses(window)
        window.module = module
        with patch.dict("sys.modules", {"curses": module}):
            run_tui(self.session)
        with self.assertRaises(TimeoutError):
            self.remote.receive()

    def test_peer_and_show_commands_use_full_screen_reader(self):
        window = FakeWindow(
            24,
            100,
            [
                "/history bob\n",
                expect(
                    lambda w: "History" in w.all_text() and "public from bob" in w.all_text(),
                    lambda w: w.module.KEY_HOME,
                ),
                expect(lambda w: "old message 0" in w.all_text(), "\x1b"),
                "\x1b",
                "/history show old\n",
                expect(lambda w: "public from carol" in w.all_text(), lambda w: w.module.KEY_HOME),
                expect(lambda w: "old message 0" in w.all_text(), "\x1b"),
                "\x1b",
                "\x03",
            ],
        )
        module = fake_curses(window)
        window.module = module
        with patch.dict("sys.modules", {"curses": module}):
            run_tui(self.session)
        with self.assertRaises(TimeoutError):
            self.remote.receive()

    def test_delete_person_is_scoped_and_invalidates_direct_cache(self):
        for server, account in [("other-server", "alice"), ("server", "eve")]:
            self.store.append(
                server, account, "received", "CHAT_PRIVATE", "bob", "keep", session="old"
            )
        browser = HistoryBrowser(self.session, ("people",))
        revision = self.session.client.direct_revision
        browser.request_delete()
        self.assertEqual(len(self.session.history_archive("bob")), 63)
        browser.confirm_delete()
        self.assertEqual(self.session.history_archive("bob"), [])
        self.assertEqual(self.session.direct_transcript("bob"), [])
        self.assertGreater(self.session.client.direct_revision, revision)
        self.assertEqual(
            [row[-1] for row in self.session.history_archive()],
            ["public from carol", "my public message"],
        )
        self.assertEqual(len(self.store.archive("other-server", "alice", "bob")), 1)
        self.assertEqual(len(self.store.archive("server", "eve", "bob")), 1)
        with self.assertRaises(TimeoutError):
            self.remote.receive()

    def test_delete_session_and_clear_current_preserve_correct_metadata(self):
        browser = HistoryBrowser(self.session)
        browser.move(1)
        browser.request_delete()
        browser.confirm_delete()
        self.assertEqual([r.id for r in self.session.history_sessions()], ["current"])
        self.assertEqual(len(self.session.history_archive()), 4)
        original_start = self.session.history_sessions()[0].started
        browser.request_delete()
        browser.confirm_delete()
        current = self.session.history_sessions()[0]
        self.assertEqual(
            (current.id, current.count, current.started), ("current", 0, original_start)
        )
        self.session.client._remember_chat("received", "CHAT_PRIVATE", "bob", "after clear")
        self.assertEqual(self.session.history_sessions()[0].count, 1)
        self.assertEqual(self.session.history_sessions()[0].started, original_start)
        with self.assertRaises(ValueError):
            self.store.delete("server", "alice")

    def test_delete_filtered_session_does_not_remove_other_peers_or_logins(self):
        browser = HistoryBrowser(self.session, ("show", "old", "bob"))
        browser.request_delete()
        browser.confirm_delete()
        self.assertEqual(
            [row[-1] for row in self.store.transcript("server", "alice", "old")],
            ["public from carol"],
        )
        self.assertEqual(len(self.session.history_archive("bob")), 3)
        self.assertEqual(len(self.session.history_sessions()), 2)

    def test_delete_requires_confirmation_and_refreshes_picker(self):
        def unchanged(window):
            self.assertEqual(len(self.store.transcript("server", "alice", "old")), 61)
            return click("[Delete]")(window)

        window = FakeWindow(
            24,
            100,
            [
                "/history\n",
                click("61 msgs"),
                click("[Delete]"),
                expect(lambda w: "cannot be undone" in w.all_text(), "\n"),
                expect(lambda w: "cannot be undone" in w.all_text(), "\x1b"),
                unchanged,
                expect(lambda w: "cannot be undone" in w.all_text(), "y"),
                expect(
                    lambda w: (
                        "Deleted 61 saved messages" in w.all_text()
                        and "61 msgs" not in w.all_text()
                    ),
                    "\x1b",
                ),
                "\x03",
            ],
        )
        module = fake_curses(window)
        window.module = module
        with patch.dict("sys.modules", {"curses": module}):
            run_tui(self.session)
        self.assertEqual(self.store.transcript("server", "alice", "old"), [])

    def test_empty_archive_and_disabled_history(self):
        self.session.client.username = "new-user"
        browser = HistoryBrowser(self.session)
        self.assertEqual(browser.people, [])
        browser.open()
        browser.open()  # Empty sessions do not index a nonexistent item.
        self.assertEqual(browser.level, "sessions")
        self.session.client.history = None
        with self.assertRaisesRegex(ValueError, "unavailable"):
            HistoryBrowser(self.session)

    def test_compact_notice_does_not_rewrite_message_body(self):
        self.session.client._notices.append(
            Notice("chat", "[CHAT_PRIVATE] bob: explain [CHAT_GLOBAL] please")
        )
        self.session._drain()
        self.assertEqual(self.output, ["[DM] bob: explain [CHAT_GLOBAL] please"])


class MessageCardTests(unittest.TestCase):
    def test_sender_boundaries_and_unicode_wrapping(self):
        for width in (16, 40, 76):
            rows = message_rows("12:30", "bob", "你好 " * 20, "DM", width, "alice")
            self.assertTrue(all(cell_width(line) <= width for _, line, _ in rows))
            self.assertTrue(rows[0][1].startswith("● bob"))
            self.assertEqual(rows[-1][1], "")
        rows = message_rows("12:30", "you -> bob", "hello", "DM", 76, "alice")
        self.assertIn("You -> bob", rows[0][1])
        self.assertEqual(rows[0][2], "accent")


if __name__ == "__main__":
    unittest.main()
