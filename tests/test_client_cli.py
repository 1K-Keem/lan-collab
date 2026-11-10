import unittest

from lancollab.client.application import ClientState
from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.common.contracts import Envelope
from lancollab.common.fake_transport import FakeTransport


class CLITests(unittest.TestCase):
    def test_startup_uses_bundled_certificate_without_flag(self):
        from unittest.mock import patch
        from lancollab.client.__main__ import main
        from lancollab.common.tls import BUNDLED_SERVER_CERT

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "lancollab.client.__main__.prompt_welcome", return_value=("login", "alice", "test")
            ),
            patch("lancollab.client.__main__.DiscoveryClient"),
            patch("lancollab.client.__main__.SocketTransport.connect_verified") as connect,
            patch("lancollab.client.__main__.ConsoleSession") as session,
            patch("lancollab.client.__main__._ask_remember_me", return_value=False),
            patch("lancollab.client.__main__.TokenStore") as token_store,
        ):
            token_store.return_value.load.return_value = None
            self.assertEqual(main(["--host", "127.0.0.1"]), 0)
            connect.assert_called_once_with(
                host="127.0.0.1",
                port=40000,
                server_hostname="lan-collab.local",
                trusted_cert=str(BUNDLED_SERVER_CERT),
            )
            session.return_value.authenticate.assert_called_once_with(
                "alice", "test", remember_me=False
            )

    def test_invalid_explicit_certificate_fails_before_credentials(self):
        from unittest.mock import patch
        from lancollab.client.__main__ import main

        with (
            patch("lancollab.client.__main__.prompt_welcome") as prompt,
            patch("sys.stderr"),
            self.assertRaises(SystemExit) as error,
        ):
            main(["--host", "127.0.0.1", "--trusted-cert", "/nonexistent/server-public.pem"])
        self.assertEqual(error.exception.code, 2)
        prompt.assert_not_called()

    def test_command_grammar(self):
        self.assertEqual(parse_command("/all hi there").args, ("hi there",))
        self.assertEqual(
            parse_command("/msg bob message with spaces").args, ("bob", "message with spaces")
        )
        self.assertEqual(
            parse_command(r'/send bob "C:\Users\Me\file.txt"').args,
            ("bob", r"C:\Users\Me\file.txt"),
        )
        for command in ("/msg bob", "/unknown", "/quit x", "/send bob", ""):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_command(command)

    def test_authenticate_sends_auth_as_first_message(self):
        from unittest.mock import patch
        from lancollab.client.application import ClientState

        a, b = FakeTransport.pair()
        session = ConsoleSession(a, output=lambda _: None)

        def respond(state, *, timeout):
            self.assertEqual(state, ClientState.AUTHENTICATED)
            request = b.receive()
            self.assertEqual(request.type, "AUTH_REQUEST")
            session.client.handle_event(
                Envelope(
                    "AUTH_RESULT",
                    request.request_id,
                    {"ok": True, "username": "alice", "role": "member"},
                )
            )

        with (
            patch.object(session, "start_reader"),
            patch.object(session, "_wait_for", side_effect=respond),
        ):
            session.authenticate("alice", "synthetic-password")
        self.assertEqual(session.client.state, ClientState.AUTHENTICATED)
        session.close()

    def test_logout_sends_logout_and_forgets_token(self):
        a, b = FakeTransport.pair()
        messages = []
        session = ConsoleSession(a, output=messages.append)
        client = session.client
        auth = client.login("alice", "synthetic-password")
        b.receive()
        client.handle_event(
            Envelope("AUTH_RESULT", auth, {"ok": True, "username": "alice", "role": "member"})
        )
        session.execute(parse_command("/logout"))
        self.assertEqual(b.receive().type, "LOGOUT")
        self.assertEqual(session.client.state, ClientState.CLOSED)

    def test_logout_forgets_persisted_token_even_after_disconnect(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from lancollab.client.token_store import TokenStore

        for failure in ("none", "send_failure", "already_closed"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                store = TokenStore(directory=Path(directory))
                store._keyring = None  # Isolated file backend; never touch the OS keyring.
                store.save("test-server", "alice", "synthetic-token")
                a, b = FakeTransport.pair()
                notices = []
                session = ConsoleSession(
                    a, output=notices.append, token_store=store, server_host="test-server"
                )
                rid = session.client.login("alice", "synthetic-password")
                b.receive()
                session.client.handle_event(
                    Envelope(
                        "AUTH_RESULT", rid, {"ok": True, "username": "alice", "role": "member"}
                    )
                )
                if failure == "send_failure":
                    b.close()
                elif failure == "already_closed":
                    session.client.close()
                self.assertFalse(session.execute(parse_command("/logout")))
                self.assertIsNone(store.load("test-server"))
                self.assertEqual(session.client.state, ClientState.CLOSED)
                if failure == "none":
                    self.assertEqual(b.receive().type, "LOGOUT")
                else:
                    self.assertTrue(any("revocation could not be confirmed" in n for n in notices))
                session.close()

    def test_quit_preserves_persisted_token(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from lancollab.client.token_store import TokenStore

        with TemporaryDirectory() as directory:
            store = TokenStore(directory=Path(directory))
            store._keyring = None
            store.save("test-server", "alice", "synthetic-token")
            a, b = FakeTransport.pair()
            session = ConsoleSession(
                a, output=lambda _: None, token_store=store, server_host="test-server"
            )
            self.assertFalse(session.execute(parse_command("/quit")))
            self.assertEqual(store.load("test-server"), ("alice", "synthetic-token"))
            with self.assertRaises(TimeoutError):
                b.receive()
            session.close()

    def test_fake_chat_and_safe_display(self):
        a, b = FakeTransport.pair()
        messages = []
        session = ConsoleSession(a, output=messages.append)
        client = session.client
        auth = client.login("alice", "synthetic-password")
        b.receive()
        client.handle_event(
            Envelope("AUTH_RESULT", auth, {"ok": True, "username": "alice", "role": "member"})
        )
        client.handle_event(
            Envelope(
                "USER_LIST",
                "snapshot-1",
                {"users": [{"username": "bob", "ip": "192.0.2.1", "port": 50000}]},
            )
        )
        self.assertTrue(session.execute(parse_command("/users")))
        self.assertIn("bob  192.0.2.1:50000", messages)
        session.execute(parse_command("/all hello"))
        self.assertEqual(b.receive().type, "CHAT_GLOBAL")
        session.execute(parse_command("/msg bob hi"))
        self.assertEqual(b.receive().payload, {"to": "bob", "text": "hi"})
        client.handle_event(
            Envelope("CHAT_GLOBAL", "event-2", {"from": "bob", "text": "\x1b[31mred"})
        )
        session._drain()
        self.assertTrue(any(r"\u001b" in message for message in messages))
        session.execute(parse_command("/quit"))
        self.assertEqual(session.client.state, ClientState.CLOSED)
        with self.assertRaises(TimeoutError):
            b.receive()  # /quit closes without LOGOUT: the token is kept


if __name__ == "__main__":
    unittest.main()
