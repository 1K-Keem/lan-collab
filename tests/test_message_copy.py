"""Exercise real TUI rendering and keys with a mocked clipboard."""

import unittest
from unittest.mock import patch

from lancollab.client.application import ChatDisplay, Notice
from lancollab.client.clipboard import ClipboardError
from tests.test_tui_context_menu import FakeSession, click, expect, run
from tests import test_history_browser
from lancollab.common.contracts import Envelope


def publish(session, text, body):
    def event(window):
        session.output(ChatDisplay(text, body))
        return window.module.KEY_RESIZE

    return event


class CopyInteractionTests(unittest.TestCase):
    def test_click_copies_original_body_and_keeps_draft(self):
        session = FakeSession()
        body = "  Xin chào\nsecond line " + "wrapped text " * 15
        with patch("lancollab.client.tui.copy_text") as copy:
            run(
                session,
                [
                    publish(session, "[All] bob: " + body, body),
                    "draft",
                    click("wrapped text"),
                    expect(lambda w: "Copy message" in w.all_text(), "c"),
                    expect(lambda w: "Copied to clipboard." in w.all_text(), "\x1b"),
                    expect(lambda w: "draft" in w.input_text(), "c"),
                    expect(lambda w: "draftc" in w.input_text(), "\x03"),
                ],
            )
        copy.assert_called_once_with(body)
        self.assertEqual(session.executed, [])

    def test_keyboard_selection_is_snapshot_even_when_new_messages_arrive(self):
        session = FakeSession()
        with patch("lancollab.client.tui.copy_text") as copy:
            run(
                session,
                [
                    publish(session, "[All] bob: first", "first"),
                    publish(session, "[All] carol: second", "second"),
                    lambda w: w.module.KEY_F9,
                    publish(session, "[All] bob: later", "later"),
                    expect(lambda w: "2 of 2" in w.all_text(), lambda w: w.module.KEY_UP),
                    "\n",
                    "\x1b",
                    "\x03",
                ],
            )
        copy.assert_called_once_with("first")

    def test_direct_copy_and_error_feedback(self):
        session = FakeSession(
            conversations=[("bob", "2026-01-01", "body")],
            transcripts={
                "bob": [("2026-01-01", "received", "CHAT_PRIVATE", "bob", "private body")]
            },
        )
        session.display_filter = "dm"
        with patch(
            "lancollab.client.tui.copy_text", side_effect=ClipboardError("Clipboard unavailable.")
        ) as copy:
            run(
                session,
                [
                    "\n",
                    lambda w: w.module.KEY_F9,
                    click("[Copy message]"),
                    expect(lambda w: "Clipboard unavailable." in w.all_text(), "\x1b"),
                    expect(lambda w: "[Back]" in w.all_text(), "\x03"),
                ],
            )
        copy.assert_called_once_with("private body")
        self.assertEqual(session.executed, [])

    def test_empty_view_does_not_copy(self):
        with patch("lancollab.client.tui.copy_text") as copy:
            run(
                FakeSession(), [lambda w: w.module.KEY_F9, "c", "\x1b", "\x03"], columns=40, rows=12
            )
        copy.assert_not_called()


class HistoryCopyTests(unittest.TestCase):
    # Reuse the isolated archive fixture without inheriting all its tests.
    setUp = test_history_browser.HistoryBrowserTests.setUp

    def test_history_click_and_keyboard_return_to_same_page(self):
        with patch("lancollab.client.tui.copy_text") as copy:
            run(
                self.session,
                [
                    lambda w: w.module.KEY_F8,
                    "\n",
                    click("hello now"),
                    "c",
                    "\x1b",
                    expect(lambda w: "hello now" in w.all_text(), lambda w: w.module.KEY_F9),
                    "\n",
                    "\x1b",
                    "\x1b",
                    "\x1b",
                    "\x03",
                ],
            )
        self.assertEqual(
            [call.args[0] for call in copy.call_args_list], ["hello now", "my public message"]
        )

    def test_people_history_separators_never_become_messages(self):
        with patch("lancollab.client.tui.copy_text") as copy:
            run(
                self.session,
                [
                    "/history bob",
                    "\n",
                    lambda w: w.module.KEY_HOME,
                    click("Session ·"),
                    expect(lambda w: "History" in w.all_text(), click("old message 0")),
                    "c",
                    "\x1b",
                    "\x1b",
                    "\x1b",
                    "\x03",
                ],
            )
        copy.assert_called_once_with("old message 0")

    def test_console_chat_keeps_original_body_metadata(self):
        raw = "  Unicode 世界\n\ttext"
        self.session.client._notices.append(Notice("chat", "[CHAT_GLOBAL] bob: displayed", raw))
        self.session._drain()
        self.assertEqual(str(self.output[-1]), "[All] bob: displayed")
        self.assertEqual(self.output[-1].copy_text, raw)

    def test_routed_and_acknowledged_messages_keep_raw_text(self):
        raw = "  original 世界\n\t\x1b[31mtext"
        client = self.session.client
        client.handle_event(
            Envelope("CHAT_PRIVATE", "incoming", {"from": "bob", "text": raw, "queued": True})
        )
        self.session._drain()
        self.assertEqual(self.output[-1].copy_text, raw)
        self.assertNotIn("\x1b", self.output[-1])
        self.assertIn("sent while you were offline", self.output[-1])
        for send in (lambda: client.send_private("bob", raw), lambda: client.send_global(raw)):
            request_id = send()
            client.handle_event(Envelope("CHAT_ACK", request_id, {"status": "queued"}))
            self.session._drain()
            self.assertEqual(self.output[-1].copy_text, raw)

    def test_history_copy_preserves_scrolled_position_and_resize(self):
        with patch("lancollab.client.tui.copy_text") as copy:
            run(
                self.session,
                [
                    "/history bob",
                    "\n",
                    lambda w: w.module.KEY_HOME,
                    lambda w: w.module.KEY_NPAGE,
                    click("[Copy]"),
                    lambda w: w.module.KEY_HOME,
                    lambda w: w.module.KEY_RESIZE,
                    "c",
                    "\x1b",
                    expect(lambda w: "old message 7" in w.all_text(), "\x1b"),
                    "\x1b",
                    "\x03",
                ],
            )
        copy.assert_called_once_with("old message 0")
