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
    def test_help_and_quit_use_shared_commands(self):
        from lancollab.client.tui import run_tui

        transport, _ = FakeTransport.pair()
        output = Mock()
        session = ConsoleSession(transport, output=output)
        session.client.state = ClientState.AUTHENTICATED
        session.client.username = "alice"
        session.client.role = "member"
        window = Mock()
        window.getmaxyx.return_value = (24, 80)
        window.get_wch.side_effect = list("/help\n/quit\n")
        with patch("curses.wrapper", side_effect=lambda function: function(window)):
            run_tui(session)
        self.assertIs(session.client.state, ClientState.CLOSED)
        self.assertIs(session.output, output)
        drawn = [call.args[2] for call in window.addnstr.call_args_list]
        self.assertIn("/send USER PATH", " ".join(drawn))


if __name__ == "__main__":
    unittest.main()
