import json
import socket
import unittest

from lancollab.client.discovery import DiscoveryClient, ServerEndpoint, _decode, select_server


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

    def test_timeout_manual_fallback(self):
        self.assertIsNone(self.client.discover())
        selected = select_server(None, host="192.0.2.100", port=40500)
        self.assertFalse(selected.discovered)
        self.assertEqual(selected.tcp_port, 40500)
        with self.assertRaises(ConnectionError):
            select_server(None, host=None)

    def test_strict_datagram_validation(self):
        self.assertIsNone(_decode(b'{"version":1,"version":1}'))
        self.assertIsNone(_decode(b"x" * 1201))
        self.assertIsNone(_decode(b"[123]"))

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
