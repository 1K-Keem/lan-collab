import unittest
from unittest.mock import Mock
from lancollab.common.socket_transport import SocketTransport


class TestSocketTransport(unittest.TestCase):
    def test_recv_exact_handles_fragmented_tcp_packets(self):
        # 1. Create a fake socket that doesn't actually connect to the internet
        mock_sock = Mock()

        # 2. Simulate TCP fragmentation: 4 bytes arriving one at a time
        mock_sock.recv.side_effect = [b"\x00", b"\x00", b"\x01", b"\x05"]

        # 3. Initialize transport and ask it to read exactly 4 bytes
        transport = SocketTransport(mock_sock)
        result = transport._recv_exact(4)

        # 4. Verify the transport loop reassembled the fragmented pieces correctly
        self.assertEqual(result, b"\x00\x00\x01\x05")

        # Verify it actually called recv() 4 separate times to get all the data
        self.assertEqual(mock_sock.recv.call_count, 4)

    def test_recv_exact_detects_closed_connection(self):
        # Simulate a connection drop (socket returns empty bytes)
        mock_sock = Mock()
        mock_sock.recv.return_value = b""

        transport = SocketTransport(mock_sock)
        with self.assertRaises(ConnectionError):
            transport._recv_exact(4)
