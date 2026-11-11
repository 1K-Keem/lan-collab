"""TUI polish: dim timestamps, per-sender colors, Ctrl+W, /history all suggestion."""

import unittest

from lancollab.client.tui import (
    SENDER_STYLES,
    Composer,
    chat_sender,
    sender_style_name,
    suggestions,
    wrap_prefixed,
)


class WrapPrefixedTests(unittest.TestCase):
    def test_prefix_only_on_first_line(self):
        rows = wrap_prefixed("17:23  ", "hello world", 10)
        self.assertEqual(rows[0][0], "17:23  ")
        self.assertTrue(all(prefix == "" for prefix, _ in rows[1:]))
        self.assertEqual("".join(line for _, line in rows), "hello world")

    def test_short_message_is_one_row(self):
        self.assertEqual(wrap_prefixed("17:23  ", "hi", 76), [("17:23  ", "hi")])

    def test_no_prefix_when_stamp_empty(self):
        self.assertEqual(
            wrap_prefixed("", "hello world", 5), [("", "hello"), ("", " worl"), ("", "d")]
        )


class ChatSenderTests(unittest.TestCase):
    def test_global_and_private_senders(self):
        self.assertEqual(chat_sender("[CHAT_GLOBAL] bob: hello"), "bob")
        self.assertEqual(chat_sender("[CHAT_PRIVATE] alice: hi"), "alice")
        self.assertEqual(chat_sender("[CHAT_PRIVATE] you -> bob: hi"), "you -> bob")

    def test_notice_prefix_before_chat_kind(self):
        # Real notices look like "[chat] [CHAT_GLOBAL] bob: hello".
        self.assertEqual(chat_sender("[chat] [CHAT_GLOBAL] bob: hello"), "bob")
        self.assertEqual(chat_sender("[chat] [CHAT_PRIVATE] you -> bob: hi"), "you -> bob")

    def test_non_chat_lines_have_no_sender(self):
        self.assertIsNone(chat_sender("[presence] bob: online"))
        self.assertIsNone(chat_sender("COMMAND HELP"))
        self.assertIsNone(chat_sender(""))

    def test_own_messages_use_accent_and_others_are_stable(self):
        self.assertEqual(sender_style_name("you", "alice"), "accent")
        self.assertEqual(sender_style_name("you -> bob", "alice"), "accent")
        self.assertEqual(sender_style_name("bob", "alice"), sender_style_name("bob", "alice"))
        self.assertIn(sender_style_name("bob", "alice"), SENDER_STYLES)
        self.assertIsNone(sender_style_name(None, "alice"))


class ComposerWordTests(unittest.TestCase):
    def test_delete_word_removes_word_and_spaces(self):
        composer = Composer()
        composer.replace("/msg bob hello ")
        composer.delete_word()
        self.assertEqual(composer.text, "/msg bob ")
        composer.delete_word()
        self.assertEqual(composer.text, "/msg ")
        composer.delete_word()
        self.assertEqual(composer.text, "")

    def test_delete_word_at_start_and_mid_line(self):
        composer = Composer()
        composer.delete_word()
        self.assertEqual(composer.text, "")
        composer.replace("hello world")
        composer.cursor = 5  # After "hello".
        composer.delete_word()
        self.assertEqual(composer.text, " world")
        self.assertEqual(composer.cursor, 0)


class SuggestionAllTests(unittest.TestCase):
    def test_history_suggests_all_sessions(self):
        found = [s.value for s in suggestions("/history ", users=("bob",), role="member")]
        self.assertIn("/history all", found)
        self.assertIn("/history bob", found)


if __name__ == "__main__":
    unittest.main()
