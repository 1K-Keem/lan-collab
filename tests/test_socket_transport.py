"""SocketTransport over real TCP sockets (loopback): framing, deadlines, serialization."""

import socket
import struct
import threading
import time
import unittest

from lancollab.common.codec import FrameError, JsonFrameCodec, MessageError
from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import SocketTransport


def tcp_pair() -> tuple[socket.socket, socket.socket]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return client, server


def chat(n: int, text: str = "hi") -> Envelope:
    return Envelope("CHAT_GLOBAL", f"r{n}", {"text": f"{text}{n}"})


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_client, self.raw_server = tcp_pair()
        self.client = SocketTransport(self.raw_client, "client")
        self.server = SocketTransport(self.raw_server, "server", frame_read_timeout=1.0)
        self.codec = JsonFrameCodec("client")
        self.addCleanup(self.client.close)
        self.addCleanup(self.server.close)

    def test_round_trip_both_directions(self) -> None:
        self.client.send(chat(1))
        self.assertEqual(self.server.receive(timeout=2), chat(1))
        reply = Envelope("CHAT_ACK", "r1", {"status": "queued"})
        self.server.send(reply)
        self.assertEqual(self.client.receive(timeout=2), reply)

    def test_byte_by_byte_fragmentation(self) -> None:
        wire = self.codec.encode(chat(1, "fragmented-"))
        sender = threading.Thread(target=self._dribble, args=(wire,))
        sender.start()
        self.assertEqual(self.server.receive(timeout=2), chat(1, "fragmented-"))
        sender.join()

    def _dribble(self, wire: bytes) -> None:
        for i in range(len(wire)):
            self.raw_client.sendall(wire[i : i + 1])
            time.sleep(0.001)

    def test_many_frames_coalesced_into_one_write(self) -> None:
        blob = b"".join(self.codec.encode(chat(n)) for n in range(50))
        self.raw_client.sendall(blob)
        self.assertEqual(
            [self.server.receive(timeout=2) for _ in range(50)], [chat(n) for n in range(50)]
        )

    def test_frame_split_across_prefix_boundary(self) -> None:
        wire = self.codec.encode(chat(7))
        self.raw_client.sendall(wire[:2])
        time.sleep(0.05)
        self.raw_client.sendall(wire[2:])
        self.assertEqual(self.server.receive(timeout=2), chat(7))

    def test_receive_timeout_consumes_nothing(self) -> None:
        with self.assertRaises(TimeoutError):
            self.server.receive(timeout=0.1)
        self.client.send(chat(1))  # transport still usable and in sync
        self.assertEqual(self.server.receive(timeout=2), chat(1))

    def test_stalled_partial_frame_hits_absolute_deadline_and_closes(self) -> None:
        wire = self.codec.encode(chat(1))
        self.raw_client.sendall(wire[:6])  # prefix + 2 body bytes, then silence
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.server.receive(timeout=5)
        self.assertLess(time.monotonic() - started, 2.5)  # frame_read_timeout=1.0
        self.assertTrue(self.server.closed)

    def test_trickling_bytes_cannot_extend_the_deadline(self) -> None:
        wire = self.codec.encode(chat(1, "x" * 200))
        stop = threading.Event()

        def trickle() -> None:
            for i in range(len(wire)):
                if stop.is_set():
                    return
                try:
                    self.raw_client.sendall(wire[i : i + 1])
                except OSError:
                    return
                time.sleep(0.02)  # ~4s total, each gap far below the 1s deadline

        thread = threading.Thread(target=trickle)
        thread.start()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.server.receive(timeout=5)
        stop.set()
        thread.join()
        self.assertLess(time.monotonic() - started, 2.5)

    def test_hostile_length_rejected_without_reading_body(self) -> None:
        for length in (0, 262145, 0xFFFFFFFF):
            raw_c, raw_s = tcp_pair()
            server = SocketTransport(raw_s, "server")
            raw_c.sendall(struct.pack(">I", length))  # no body follows
            with self.assertRaises(FrameError):
                server.receive(timeout=2)
            self.assertTrue(server.closed)
            raw_c.close()

    def test_invalid_json_closes_but_semantic_error_keeps_stream_in_sync(self) -> None:
        bad = b'{"version":1,"type":"AUTH_REQUEST","request_id":"r1","payload":{}}'
        self.raw_client.sendall(struct.pack(">I", len(bad)) + bad)
        self.client.send(chat(2))
        with self.assertRaises(MessageError) as ctx:
            self.server.receive(timeout=2)
        self.assertEqual(ctx.exception.request_id, "r1")
        self.assertFalse(self.server.closed)
        self.assertEqual(self.server.receive(timeout=2), chat(2))
        garbage = b"not json"
        self.raw_client.sendall(struct.pack(">I", len(garbage)) + garbage)
        with self.assertRaises(FrameError):
            self.server.receive(timeout=2)
        self.assertTrue(self.server.closed)

    def test_peer_close_raises_connection_error(self) -> None:
        self.raw_client.close()
        with self.assertRaises(ConnectionError):
            self.server.receive(timeout=2)

    def test_close_wakes_a_blocked_reader(self) -> None:
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                self.server.receive()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.1)
        self.server.close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], ConnectionError)

    def test_concurrent_senders_never_interleave_frames(self) -> None:
        per_thread, threads = 200, 4

        def spam(tag: int) -> None:
            for n in range(per_thread):
                self.client.send(
                    Envelope("CHAT_GLOBAL", f"{tag}-{n}", {"text": "z" * 500 + str(tag)})
                )

        workers = [threading.Thread(target=spam, args=(t,)) for t in range(threads)]
        for w in workers:
            w.start()
        seen: dict[int, list[int]] = {t: [] for t in range(threads)}
        for _ in range(per_thread * threads):
            msg = self.server.receive(timeout=5)
            tag, n = msg.request_id.split("-")
            self.assertTrue(msg.payload["text"].endswith(tag))
            seen[int(tag)].append(int(n))
        for w in workers:
            w.join()
        for tag, order in seen.items():
            self.assertEqual(order, list(range(per_thread)), f"thread {tag} reordered")

    def test_write_deadline_closes_transport_when_peer_never_reads(self) -> None:
        raw_c, raw_s = tcp_pair()
        raw_c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        raw_s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stuck = SocketTransport(raw_c, "client", write_timeout=0.5)
        self.addCleanup(stuck.close)
        self.addCleanup(raw_s.close)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            for n in range(100_000):  # the peer never reads: buffers fill, then deadline hits
                stuck.send(Envelope("CHAT_GLOBAL", f"r{n}", {"text": "y" * 4000}))
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(stuck.closed)

    def test_send_validates_before_touching_the_wire(self) -> None:
        from lancollab.common.codec import OutboundMessageError

        with self.assertRaises(OutboundMessageError):
            self.client.send(Envelope("AUTH_RESULT", "r1", {"ok": True, "username": "alice"}))
        with self.assertRaises(TimeoutError):  # nothing was written
            self.server.receive(timeout=0.1)


if __name__ == "__main__":
    unittest.main()
