"""Run with python -m unittest tests.test_network_status."""

from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from lancollab.client.application import ClientState
from lancollab.client.cli import ConsoleSession, network_status, parse_command
from lancollab.client.discovery import NetworkQuality
from tests.fake_transport import FakeTransport
from tests.test_tui_context_menu import FakeSession, expect, run


class NetworkStatusTests(unittest.TestCase):
    def session(self):
        session = FakeSession()
        session.client.transport = SimpleNamespace(peer_address=("206.189.94.234", 40500))
        session.server_host = "server.example"
        session.udp = SimpleNamespace(quality=NetworkQuality(0, 0, 0, None))
        return session

    def test_labels_distinguish_unmeasured_loss_and_disconnection(self):
        session = self.session()
        client = session.client
        labels = network_status(client, session.udp, session.server_host)
        self.assertEqual(labels, (
            "Server: 206.189.94.234:40500",
            "TCP/TLS: connected",
            "RTT: not measured",
            "UDP probe loss: not measured",
        ))
        client.last_tcp_rtt = 12.5
        session.udp.quality = NetworkQuality(2, 0, 100, None)
        labels = network_status(client, session.udp)
        self.assertEqual(labels[1], "TCP/TLS: connected")
        self.assertEqual(labels[2], "RTT: 12.5 ms")
        self.assertEqual(labels[3], "UDP probe loss: 100.0% (0/2)")
        client.state = ClientState.CLOSED
        labels = network_status(client, session.udp)
        self.assertEqual(labels[1], "TCP/TLS: disconnected")
        self.assertEqual(labels[2], "RTT: 12.5 ms (last)")
        self.assertEqual(network_status(client)[3], "UDP probe loss: unavailable")
        client.transport.peer_address = None
        self.assertEqual(network_status(client, server_host="server.example")[0],
                         "Server: server.example")

    def test_status_command_uses_the_same_labels(self):
        transport, _ = FakeTransport.pair()
        transport.peer_address = ("192.168.1.10", 40500)
        output = []
        with TemporaryDirectory() as directory:
            session = ConsoleSession(transport, output=output.append, downloads_dir=directory)
            self.addCleanup(session.close)
            session.client.state = ClientState.AUTHENTICATED
            session.execute(parse_command("/status"))
            self.assertEqual(output, list(network_status(session.client)))

    def test_status_stays_visible_on_small_screens_and_without_sidebar(self):
        for rows, columns in ((24, 76), (28, 120)):
            with self.subTest(columns=columns):
                session = self.session()

                def visible(window):
                    text = window.all_text()
                    return all(label in text for label in (
                        "Server: 206.189.94.234:40500",
                        "TCP/TLS: connected",
                        "RTT: not measured",
                        "UDP probe loss: not measured",
                    ))

                run(session, [
                    expect(visible, lambda w: w.module.KEY_F2),
                    expect(visible, "\x03"),
                ], rows=rows, columns=columns)

    def test_disconnect_remains_visible_until_acknowledged(self):
        session = self.session()

        def disconnect(window):
            session.client.state = ClientState.CLOSED
            raise window.module.error("no input")

        run(session, [
            disconnect,
            expect(lambda w: "TCP/TLS: disconnected" in w.all_text()
                   and "press any key to exit" in w.all_text(), "\n"),
        ])
        self.assertEqual(session.executed, [])


if __name__ == "__main__":
    unittest.main()
