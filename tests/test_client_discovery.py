import json
import socket
import unittest

from lancollab.client.discovery import (
    DiscoveryClient,
    ServerEndpoint,
    broadcast_targets,
    select_server,
)
from lancollab.server.connections import address_banner


class FakeDatagramSocket:
    def __init__(self):
        self.events = []
        self.sent = []
        self.bound = None
        self.closed = False

    def setsockopt(self, *_args):
        pass

    def bind(self, address):
        self.bound = address

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, data, address):
        self.sent.append((json.loads(data), address))

    def recvfrom(self, _max_bytes):
        if self.events:
            return self.events.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True

    def queue(self, payload, ip="192.0.2.21"):
        self.events.append((json.dumps(payload).encode(), (ip, 40001)))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.sock = FakeDatagramSocket()
        self.time = [0.0]
        self.client = DiscoveryClient(
            socket_factory=lambda *_args: self.sock,
            clock=lambda: self.time[0],
        )

    def tearDown(self):
        self.client.close()

    def test_ephemeral_source_and_discover(self):
        self.assertEqual(self.sock.bound, ("", 0))
        self.sock.queue(
            {
                "version": 1,
                "type": "DISCOVER_REPLY",
                "instance_id": "wrong",
                "server_name": "other",
                "tcp_port": 1,
            }
        )
        self.sock.queue(
            {
                "version": 1,
                "type": "DISCOVER_REPLY",
                "instance_id": self.client.instance_id,
                "server_name": "test-server",
                "tcp_port": 40000,
            }
        )
        found = self.client.discover()
        self.assertEqual(found, ServerEndpoint("192.0.2.21", 40000, "test-server", True))
        self.assertEqual(self.sock.sent[0][0]["type"], "DISCOVER")
        self.assertEqual(self.sock.sent[0][1], ("255.255.255.255", 40001))

    def test_broadcast_targets_include_directed_subnets(self):
        targets = broadcast_targets(["10.127.5.255", "192.168.56.1", "127.0.0.1", "8.8.8.8"])
        self.assertEqual(targets[0], "255.255.255.255")
        self.assertIn("10.127.255.255", targets)  # campus Wi-Fi /16
        self.assertIn("192.168.56.255", targets)  # VM host-only /24
        self.assertNotIn("10.127.5.255", targets)  # never our own address
        self.assertFalse(any(t.startswith(("127.", "8.")) for t in targets))
        self.assertEqual(len(targets), len(set(targets)))

    def test_explicit_broadcast_host_only(self):
        self.client.discover(broadcast_host="127.0.0.1")
        self.assertEqual({address for _msg, address in self.sock.sent}, {("127.0.0.1", 40001)})

    def test_discover_resends_until_reply(self):
        self.assertIsNone(self.client.discover(broadcast_host="10.0.0.255", attempts=3))
        self.assertEqual(len(self.sock.sent), 3)
        self.sock.sent.clear()
        self.sock.queue(
            {
                "version": 1,
                "type": "DISCOVER_REPLY",
                "instance_id": self.client.instance_id,
                "server_name": "test-server",
                "tcp_port": 40000,
            }
        )
        self.assertIsNotNone(self.client.discover(broadcast_host="10.0.0.255", attempts=3))
        self.assertEqual(len(self.sock.sent), 1)

    def test_server_banner_prefers_default_route_address(self):
        lines = address_banner(40000, ["10.127.29.195", "192.168.56.1"])
        self.assertIn("lancollab --host 10.127.29.195", lines[0])
        self.assertNotIn("192.168.56.1", lines[0])
        self.assertIn("192.168.56.1", lines[1])
        self.assertIn("--port 40500", address_banner(40500, ["10.0.0.5"])[0])
        self.assertIn("127.0.0.1", address_banner(40000, [])[0])

    def test_timeout_manual_fallback(self):
        self.assertIsNone(self.client.discover())
        selected = select_server(None, host="192.0.2.100", port=40500)
        self.assertFalse(selected.discovered)
        self.assertEqual(selected.tcp_port, 40500)
        with self.assertRaises(ConnectionError):
            select_server(None, host=None)

    def test_strict_datagram_validation(self):
        for raw in (
            b'{"version":1,"version":1}',
            b"x" * 1201,
            b"[123]",
            b"\xff",
            b"not json",
            b"",
        ):
            self.sock.events.append((raw, ("192.0.2.21", 40001)))
        self.sock.queue(
            {
                "version": 1,
                "type": "DISCOVER_REPLY",
                "instance_id": self.client.instance_id,
                "server_name": "test-server",
                "tcp_port": 40000,
            }
        )
        self.assertEqual(self.client.discover().host, "192.0.2.21")

    def test_heartbeat_ack_and_lost_packet(self):
        self.sock.queue(
            {
                "version": 1,
                "type": "HEARTBEAT_ACK",
                "instance_id": self.client.instance_id,
                "seq": 1,
            }
        )
        quality = self.client.heartbeat("192.0.2.21")
        self.assertEqual((quality.sent, quality.received), (1, 1))
        self.assertEqual(quality.loss_percent, 0)
        quality = self.client.heartbeat("192.0.2.21")
        self.assertEqual((quality.sent, quality.received), (2, 1))
        self.assertEqual(quality.loss_percent, 50)
        self.assertEqual(self.sock.sent[0][0]["seq"], 1)
        self.assertEqual(self.sock.sent[1][0]["seq"], 2)

    def test_wrong_udp_host_does_not_count(self):
        self.sock.queue(
            {
                "version": 1,
                "type": "HEARTBEAT_ACK",
                "instance_id": self.client.instance_id,
                "seq": 1,
            },
            ip="192.0.2.254",
        )
        quality = self.client.heartbeat("192.0.2.21")
        self.assertEqual(quality.received, 0)


if __name__ == "__main__":
    unittest.main()
