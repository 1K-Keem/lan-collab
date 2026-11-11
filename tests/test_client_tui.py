"""The TUI uses the existing command dispatcher and restores the terminal on exit."""

import unittest
from unittest.mock import Mock, patch

from lancollab.client.application import ClientState
from lancollab.client.cli import ConsoleSession
from tests.fake_transport import FakeTransport

try:
    import curses
except ImportError:
    curses = None


@unittest.skipIf(curses is None, "curses is not installed on this platform")
class TuiTests(unittest.TestCase):
    def test_help_opens_separate_screen_and_ctrl_c_returns_to_chat(self):
        from lancollab.client.tui import run_tui

        transport, _ = FakeTransport.pair()
        output = Mock()
        session = ConsoleSession(transport, output=output)
        session.client.state = ClientState.AUTHENTICATED
        session.client.username = "alice"
        session.client.role = "member"
        window = Mock()
        window.getmaxyx.return_value = (24, 80)
        # /help opens the help screen, Ctrl+C returns to chat, /quit exits.
        # Live, Ctrl+C arrives as SIGINT (KeyboardInterrupt under cbreak),
        # not as the \x03 byte, so the test raises it the same way.
        window.get_wch.side_effect = (
            list("/help\n") + [curses.KEY_NPAGE, KeyboardInterrupt] + list("/quit\n")
        )
        with patch("curses.wrapper", side_effect=lambda function: function(window)):
            run_tui(session)
        self.assertIs(session.client.state, ClientState.CLOSED)
        self.assertIs(session.output, output)
        drawn = [call.args[2] for call in window.addstr.call_args_list]
        joined = " ".join(drawn)
        self.assertIn("LAN COLLAB — Help", joined)
        self.assertIn("/send USER PATH", joined)
        self.assertIn("back to chat", joined)


class InteractionTests(unittest.TestCase):
    def test_suggestions_filter_roles_and_preserve_message_text(self):
        from lancollab.client.tui import suggestions

        self.assertEqual([s.value for s in suggestions("/m")], ["/msg"])
        self.assertNotIn("/approve", [s.value for s in suggestions("/")])
        self.assertIn("/approve", [s.value for s in suggestions("/", role="admin")])
        self.assertEqual([s.value for s in suggestions("/msg b", ["alice", "bob"])], ["/msg bob"])
        self.assertEqual(suggestions("/msg bob hello", ["bob"]), [])
        self.assertEqual(suggestions("hello"), [])

    def test_edit_cursor_and_history_restore_draft(self):
        from lancollab.client.tui import Composer

        composer = Composer()
        composer.insert("helo")
        composer.cursor = 3
        composer.insert("l")
        self.assertEqual(composer.take(), "hello")
        composer.insert("draft")
        composer.recall(-1)
        self.assertEqual(composer.text, "hello")
        composer.recall(1)
        self.assertEqual(composer.text, "draft")

    def test_unicode_wrap_respects_terminal_columns(self):
        from lancollab.client.tui import wrap_cells, cell_width, fit_cells

        text = "你好 e\u0301 world"
        rows = wrap_cells(text, 4)
        self.assertEqual("".join(rows), text)
        self.assertTrue(all(cell_width(row) <= 4 for row in rows))
        self.assertEqual(fit_cells("你好", 3), "你")

    def test_banner_plain_output_and_no_color(self):
        import io
        from lancollab.client.branding import BANNER, print_welcome

        self.assertTrue(all(line.isascii() for line in BANNER))
        stream = io.StringIO()
        print_welcome(stream)
        self.assertEqual(stream.getvalue(), "")
        stream.isatty = lambda: True
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            print_welcome(stream)
        self.assertIn("C O L L A B", stream.getvalue())
        self.assertNotIn("\x1b", stream.getvalue())

    @unittest.skipIf(curses is None, "curses is not installed")
    def test_tab_completion_username_and_plain_chat(self):
        from lancollab.client.tui import run_tui

        transport, peer = FakeTransport.pair()
        session = ConsoleSession(transport, output=lambda _: None)
        session.client.state = ClientState.AUTHENTICATED
        session.client.username = "alice"
        session.client.role = "member"
        session.client.online_users = {"bob": ("127.0.0.1", 1234)}
        window = Mock()
        window.getmaxyx.return_value = (28, 120)
        window.get_wch.side_effect = list("/m\tb\thello\nhello everyone\n/quit\n")
        with patch("curses.wrapper", side_effect=lambda fn: fn(window)):
            run_tui(session)
        private, public = peer.receive(), peer.receive()
        self.assertEqual(private.type, "CHAT_PRIVATE")
        self.assertEqual(private.payload, {"to": "bob", "text": "hello"})
        self.assertEqual(public.type, "CHAT_GLOBAL")
        self.assertEqual(public.payload, {"text": "hello everyone"})


if __name__ == "__main__":
    unittest.main()
