import unittest

from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.common.contracts import Envelope
from lancollab.common.fake_transport import FakeTransport


class CLITests(unittest.TestCase):
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

    def test_fake_chat_and_safe_display(self):
        a, b = FakeTransport.pair()
        messages = []
        session = ConsoleSession(a, output=messages.append)
        client = session.client
        hello = client.send_hello("test")
        b.receive()
        client.handle_event(
            Envelope("HELLO_ACK", hello, {"server_name": "fake", "protocol_version": 1})
        )
        auth = client.login("alice", "synthetic-password")
        b.receive()
        client.handle_event(Envelope("AUTH_RESULT", auth, {"ok": True, "username": "alice"}))
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
        self.assertEqual(b.receive().type, "LOGOUT")


if __name__ == "__main__":
    unittest.main()
