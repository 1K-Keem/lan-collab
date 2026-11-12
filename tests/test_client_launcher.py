"""The installed command keeps defaults separate from transport behavior."""

import unittest
from unittest.mock import patch

from lancollab.client.launcher import main


class LauncherTests(unittest.TestCase):
    @patch("lancollab.client.launcher.client_main", return_value=0)
    def test_default_server_and_tui(self, client):
        self.assertEqual(main([]), 0)
        self.assertEqual(
            client.call_args.args[0], ["--port", "40000", "--tui", "--host", "206.189.94.234"]
        )

    @patch("lancollab.client.launcher.client_main", return_value=2)
    def test_user_overrides_follow_defaults_and_exit_code_is_preserved(self, client):
        self.assertEqual(main(["--host", "127.0.0.1", "--port", "41000"]), 2)
        self.assertEqual(client.call_args.args[0][-4:], ["--host", "127.0.0.1", "--port", "41000"])

    @patch("lancollab.client.launcher.client_main", return_value=0)
    def test_discovery_has_no_implicit_host(self, client):
        main(["--discover"])
        self.assertNotIn("--host", client.call_args.args[0])
        self.assertIn("--discover", client.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
