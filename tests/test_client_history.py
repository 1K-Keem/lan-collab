"""Local history persists confirmed chat only, with server/account isolation."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lancollab.client.application import CollabClient, ClientState
from lancollab.client.cli import ConsoleSession, AuthenticationError, parse_command
from lancollab.client.history import HistoryStore
from lancollab.common.contracts import Envelope
from tests.fake_transport import FakeTransport


class HistoryTests(unittest.TestCase):
    def test_persistence_and_account_server_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = HistoryStore(path)
            store.append("server-a", "alice", "received", "CHAT_PRIVATE", "bob", "hello")
            store.append("server-a", "bob", "sent", "CHAT_PRIVATE", "alice", "other account")
            store.append("server-b", "alice", "sent", "CHAT_GLOBAL", "*", "other server")
            store.close()
            store = HistoryStore(path)
            self.addCleanup(store.close)
            rows = store.recent("server-a", "alice")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][-1], "hello")
            self.assertEqual(store.recent("server-a", "alice", "carol"), [])

    def test_only_acknowledged_outgoing_and_received_chat_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3")
            self.addCleanup(store.close)
            a, b = FakeTransport.pair()
            client = CollabClient(a, auth_allowed=True, history=store, history_server="server")
            self.addCleanup(client.close)
            rid = client.login("alice", "synthetic-password")
            b.receive()
            client.handle_event(
                Envelope("AUTH_RESULT", rid, {"ok": True, "username": "alice", "role": "member"})
            )
            rid = client.send_private("bob", "queued message")
            b.receive()
            self.assertEqual(store.recent("server", "alice"), [])
            client.handle_event(Envelope("CHAT_ACK", rid, {"status": "queued"}))
            rid = client.send_private("offline", "failed message")
            b.receive()
            client.handle_event(
                Envelope("ERROR", rid, {"code": "USER_OFFLINE", "message": "offline"})
            )
            client.handle_event(
                Envelope("CHAT_PRIVATE", "event", {"from": "bob", "text": "hello\x1b[31m"})
            )
            self.assertEqual(
                [row[-1] for row in store.recent("server", "alice")],
                ["queued message", "hello\x1b[31m"],
            )
            output = []
            shell = ConsoleSession(a, history=store, history_server="server", output=output.append)
            shell.client = client
            shell.execute(parse_command("/history bob"))
            self.assertTrue(any("hello\\u001b" in line for line in output))
            self.assertFalse(any("\x1b" in line for line in output))

    def test_rejected_token_correlates_and_fails_promptly(self):
        a, b = FakeTransport.pair()
        shell = ConsoleSession(a)
        self.addCleanup(shell.close)
        rid = shell.client.login_with_token("synthetic-token")
        b.receive()
        shell.client.handle_event(
            Envelope("AUTH_RESULT", rid, {"ok": False, "error_code": "TOKEN_REVOKED"})
        )
        self.assertIs(shell.client.state, ClientState.WAIT_AUTH)
        with self.assertRaises(AuthenticationError):
            shell._wait_for(ClientState.AUTHENTICATED)

    def test_connection_failure_does_not_delete_saved_login(self):
        from lancollab.client.__main__ import main

        with (
            patch("lancollab.client.__main__.TokenStore") as store,
            patch("lancollab.client.__main__.DiscoveryClient"),
            patch(
                "lancollab.client.__main__.SocketTransport.connect_verified",
                side_effect=ConnectionError("offline"),
            ),
            patch("lancollab.client.__main__.prompt_welcome") as prompt,
            patch("sys.stderr"),
        ):
            store.return_value.load.return_value = ("alice", "token")
            self.assertEqual(main(["--host", "127.0.0.1", "--no-history"]), 1)
            store.return_value.delete.assert_not_called()
            prompt.assert_not_called()

    def test_remember_prompt_precedes_connection(self):
        from lancollab.client.__main__ import main

        events = []
        with (
            patch("lancollab.client.__main__.TokenStore") as store,
            patch("lancollab.client.__main__.DiscoveryClient"),
            patch("lancollab.client.__main__.ConsoleSession"),
            patch(
                "lancollab.client.__main__.prompt_welcome",
                return_value=("login", "alice", "synthetic"),
            ),
            patch(
                "lancollab.client.__main__._ask_remember_me",
                side_effect=lambda: events.append("prompt") or False,
            ),
            patch(
                "lancollab.client.__main__.SocketTransport.connect_verified",
                side_effect=lambda **kwargs: events.append("connect"),
            ),
        ):
            store.return_value.load.return_value = None
            self.assertEqual(main(["--host", "127.0.0.1", "--no-history"]), 0)
            self.assertEqual(events, ["prompt", "connect"])


if __name__ == "__main__":
    unittest.main()
