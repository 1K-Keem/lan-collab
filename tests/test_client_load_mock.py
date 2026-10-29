"""Mock-only twenty-client request isolation; NOT a 20-client LAN benchmark."""

import unittest

from lancollab.client.application import CollabClient, ClientState
from lancollab.common.contracts import Envelope
from lancollab.common.fake_transport import FakeTransport


class TwentyMockClients(unittest.TestCase):
    def test_20_isolated_clients(self):
        clients = []
        servers = []
        try:
            for n in range(20):
                local, server = FakeTransport.pair()
                client = CollabClient(local, auth_allowed=True)
                hid = client.send_hello(f"tester{n}")
                self.assertEqual(server.receive().type, "HELLO")
                client.handle_event(
                    Envelope(
                        "HELLO_ACK",
                        hid,
                        {
                            "server_name": "fake",
                            "protocol_version": 1,
                        },
                    )
                )
                authid = client.login(f"user{n}", "synthetic-only")
                self.assertEqual(server.receive().type, "AUTH_REQUEST")
                client.handle_event(
                    Envelope(
                        "AUTH_RESULT",
                        authid,
                        {
                            "ok": True,
                            "username": f"user{n}",
                        },
                    )
                )
                self.assertEqual(client.state, ClientState.AUTHENTICATED)
                clients.append(client)
                servers.append(server)
            for n, client in enumerate(clients):
                chatid = client.send_global(f"from {n}")
                event = servers[n].receive()
                self.assertEqual(event.payload, {"text": f"from {n}"})
                client.handle_event(Envelope("CHAT_ACK", chatid, {"status": "queued"}))
                self.assertEqual(client._pending, {})
        finally:
            for client in clients:
                client.close()


if __name__ == "__main__":
    unittest.main()
