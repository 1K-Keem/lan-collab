"""TV2 pieces together over real sockets: verified TLS + codec + transport + transfer core.

``MiniRelay`` is a TEST-ONLY stand-in for TV1's server: it trusts the HELLO client_name as the
username and has no authentication, queues or presence. It exists to prove the TV2 components
interoperate under real concurrency, not to replace the production server.
"""

from typing import Any
import hashlib
import itertools
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from lancollab.common.contracts import Envelope, SessionIdentity
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context
from lancollab.common.transfer_core import (
    DiskSink,
    IncomingTransfer,
    OutgoingTransfer,
    TransferRegistry,
    TransferState,
)

try:
    from lancollab.common.devcerts import generate_dev_certs

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

CHUNK = 16384
_ids = itertools.count(1)


def rid() -> str:
    return f"q{next(_ids)}"


class MiniRelay:
    def __init__(self, context) -> None:
        self.context = context
        self.registry = TransferRegistry()
        self.lock = threading.Lock()
        self.users: dict[str, SessionIdentity] = {}
        self.transports: dict[str, SocketTransport] = {}
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.listener.close()
        for t in list(self.transports.values()):
            t.close()

    def _accept(self) -> None:
        while True:
            try:
                raw, addr = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw, addr), daemon=True).start()

    def _serve(self, raw: socket.socket, addr) -> None:
        try:
            transport = SocketTransport.accept_verified(self.context, raw)
            hello = transport.receive(timeout=5)
        except Exception:  # noqa: BLE001
            return
        identity = SessionIdentity(addr[0], addr[1], uuid.uuid4().hex, hello.payload["client_name"])
        assert identity.username is not None
        with self.lock:
            self.users[identity.username] = identity
            self.transports[identity.session_id] = transport
            self.registry.open_session(identity)
        transport.send(
            Envelope("HELLO_ACK", hello.request_id, {"server_name": "t", "protocol_version": 1})
        )
        try:
            while True:
                self._dispatch(identity, transport, transport.receive())
        except Exception:  # noqa: BLE001  EOF, reset or protocol failure => session cleanup
            pass
        finally:
            with self.lock:
                if identity.username is not None and self.users.get(identity.username) == identity:
                    del self.users[identity.username]
                self._apply(self.registry.close_session(identity.session_id))
                self.transports.pop(identity.session_id, None)
            transport.close()

    def _dispatch(self, identity, transport, message: Envelope) -> None:
        if message.type == "FILE_OFFER":
            receiver = self.users.get(message.payload["to"])
            if receiver is None:
                out = None
                transport.send(
                    Envelope(
                        "ERROR", message.request_id, {"code": "USER_OFFLINE", "message": "offline"}
                    )
                )
            else:
                out = self.registry.offer(identity, receiver, message)
        elif message.type.startswith("FILE_"):
            out = self.registry.handle(identity, message)
        else:
            return
        if out is not None:
            self._apply(out, message.request_id, transport)

    def _apply(self, out, request_id: str | None = None, reply_to=None) -> None:
        if out.error and reply_to is not None and request_id:
            reply_to.send(out.error.reply(request_id))
        for fwd in out.forwards:
            target = self.transports.get(fwd.recipient.session_id)
            if target is None:
                continue
            try:
                target.send(fwd.message)
            except (ConnectionError, TimeoutError):
                continue
            if fwd.message.type in ("FILE_OFFER", "FILE_ACCEPT", "FILE_END"):
                self.registry.mark_written(fwd.message.payload["transfer_id"], fwd.message.type)


class Peer:
    """A client: one reader thread (never blocks on credit) plus caller-driven sender."""

    def __init__(self, relay: MiniRelay, ca: str, name: str, download_dir: str, auto_accept=True):
        self.name = name
        self.dir = download_dir
        self.auto_accept = auto_accept
        self.transport = SocketTransport.connect_verified("127.0.0.1", relay.port, ca)
        self.transport.send(Envelope("HELLO", rid(), {"client_name": name}))
        self.transport.receive(timeout=5)
        self.outgoing: dict[str, OutgoingTransfer] = {}
        self.incoming: dict[str, IncomingTransfer] = {}
        self.results: dict[str, Any] = {}
        self.cancel_after_first_chunk = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _send(self, kind: str, **payload) -> None:
        self.transport.send(Envelope(kind, rid(), payload))

    def _read(self) -> None:
        try:
            while True:
                self._handle(self.transport.receive())
        except Exception:  # noqa: BLE001  connection lost: every transfer must be abandoned
            for out in self.outgoing.values():
                out.on_cancel("CONNECTION_LOST")
            for inc in self.incoming.values():
                inc.cancel()  # closes and deletes the partial file

    def _handle(self, m: Envelope) -> None:
        p = m.payload
        tid = p.get("transfer_id")
        if m.type == "FILE_OFFER" and self.auto_accept:
            assert isinstance(tid, str)
            sink = DiskSink(self.dir, p["name"])
            self.incoming[tid] = IncomingTransfer(tid, p["name"], p["size"], p["sha256"], sink)
            self._send("FILE_ACCEPT", transfer_id=tid)
        elif (
            m.type == "FILE_CHUNK" and isinstance(tid, str) and tid in self.incoming
        ):  # <--- add isinstance check
            for ack in self.incoming[tid].on_chunk(p):
                self._send("FILE_ACK", transfer_id=tid, next_offset=ack)
            if self.cancel_after_first_chunk:
                self.incoming[tid].cancel()
                self._send("FILE_CANCEL", transfer_id=tid, reason="USER_ABORT")
        elif (
            m.type == "FILE_END" and isinstance(tid, str) and tid in self.incoming
        ):  # <--- add isinstance check
            result = self.incoming[tid].on_end(p["total_chunks"])
            for ack in result.acks:
                self._send("FILE_ACK", transfer_id=tid, next_offset=ack)
            extra = {} if result.ok else {"reason": result.reason}
            self._send("FILE_RESULT", transfer_id=tid, ok=result.ok, **extra)
            self.results[tid] = result
        elif m.type == "ERROR":  # a rejected request: fail any offer still waiting for a decision
            for out in self.outgoing.values():
                if out.state is TransferState.OFFERED:
                    out.on_cancel(p["code"])
        elif m.type == "FILE_CANCEL":
            if tid in self.incoming:
                self.incoming[tid].cancel()
            if tid in self.outgoing:
                self.outgoing[tid].on_cancel(p["reason"])
        elif isinstance(tid, str) and tid in self.outgoing:  # <--- add isinstance check
            out = self.outgoing[tid]
            if m.type == "FILE_ACCEPT":
                out.on_accept()
            elif m.type == "FILE_ACK":
                out.on_ack(p["next_offset"])
            elif m.type == "FILE_RESULT":
                out.on_result(p["ok"], p.get("reason"))
            elif m.type == "FILE_REJECT":
                out.on_reject(p.get("reason"))

    def send_file(self, to: str, data: bytes, name: str = "payload.bin", timeout: float = 30):
        tid = str(uuid.uuid4())
        out = OutgoingTransfer(tid, len(data))
        self.outgoing[tid] = out
        self._send(
            "FILE_OFFER",
            transfer_id=tid,
            to=to,
            name=name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        deadline = time.monotonic() + timeout
        while out.state is TransferState.OFFERED and time.monotonic() < deadline:
            time.sleep(0.005)
        offset, peak = 0, 0
        while not out.all_sent and not out.finished and time.monotonic() < deadline:
            if not out.wait_for_credit(0.5):
                continue
            if out.finished:
                break
            piece = data[offset : offset + CHUNK]
            offset += len(piece)
            payload = out.next_chunk_payload(piece)
            peak = max(peak, out._ledger.outstanding)
            self._send("FILE_CHUNK", **payload)
        if out.all_sent and not out.finished:
            self._send("FILE_END", **out.end_payload())  # no waiting for ACK drain
        while not out.finished and time.monotonic() < deadline:
            time.sleep(0.005)
        return out, peak

    def close(self) -> None:
        self.transport.close()
        self.thread.join(timeout=2)


@unittest.skipUnless(HAVE_CRYPTO, "needs the developer-only 'cryptography' package")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.certs = generate_dev_certs(Path(cls.tmp.name) / "certs")
        cls.ctx = create_server_context(str(cls.certs.server_cert), str(cls.certs.server_key))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.relay = MiniRelay(self.ctx)
        self.addCleanup(self.relay.close)
        self.ca = str(self.certs.ca_cert)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)

    def peer(self, name: str, **kw) -> Peer:
        directory = Path(self.work.name) / name
        peer = Peer(self.relay, self.ca, name, str(directory), **kw)
        self.addCleanup(peer.close)
        return peer

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_three_mebibyte_file_over_verified_tls(self) -> None:
        alice, bob = self.peer("alice"), self.peer("bob")
        data = os.urandom(3 * 1024 * 1024 + 123)
        out, peak = alice.send_file("bob", data)
        self.assertIs(out.state, TransferState.DONE, out.reason)
        self.assertEqual(peak, 8)  # the credit window really filled, and never exceeded
        saved = next(iter(bob.results.values())).saved_to
        self.assertEqual(Path(saved).read_bytes(), data)
        self.assertTrue(self.wait_for(lambda: not self.relay.registry._active))

    def test_empty_file_and_single_byte(self) -> None:
        alice = self.peer("alice")
        self.peer("bob")
        for payload in (b"", b"z"):
            out, _ = alice.send_file("bob", payload, name="e.bin")
            self.assertIs(out.state, TransferState.DONE, out.reason)
        names = sorted(p.name for p in (Path(self.work.name) / "bob").iterdir())
        self.assertEqual(names, ["e (1).bin", "e.bin"])

    def test_twenty_concurrent_transfers_between_ten_pairs(self) -> None:
        pairs = [(self.peer(f"s{i}"), self.peer(f"r{i}")) for i in range(10)]
        blobs = [os.urandom(CHUNK * 20 + i) for i in range(10)]
        outcomes: list = [None] * 10

        def run(i: int) -> None:
            outcomes[i] = pairs[i][0].send_file(f"r{i}", blobs[i])

        threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        for i, (out, peak) in enumerate(outcomes):
            self.assertIs(out.state, TransferState.DONE, f"pair {i}: {out.reason}")
            self.assertLessEqual(peak, 8)
            saved = next(iter(pairs[i][1].results.values())).saved_to
            self.assertEqual(Path(saved).read_bytes(), blobs[i])

    def test_receiver_cancel_mid_transfer_cleans_up_both_sides(self) -> None:
        alice, bob = self.peer("alice"), self.peer("bob")
        bob.cancel_after_first_chunk = True
        out, _ = alice.send_file("bob", os.urandom(CHUNK * 64))
        self.assertIs(out.state, TransferState.CANCELLED)
        self.assertEqual(out.reason, "USER_ABORT")
        self.assertTrue(self.wait_for(lambda: not self.relay.registry._active))
        leftovers = [p for p in (Path(self.work.name) / "bob").iterdir()]
        self.assertEqual(leftovers, [])

    def test_receiver_disconnect_mid_transfer_cancels_sender(self) -> None:
        alice, bob = self.peer("alice"), self.peer("bob")

        def kill_when_data_flows() -> None:
            self.wait_for(lambda: any(i.chunks > 0 for i in bob.incoming.values()))
            bob.close()

        killer = threading.Thread(target=kill_when_data_flows)
        killer.start()
        out, _ = alice.send_file("bob", os.urandom(CHUNK * 3000), timeout=20)
        killer.join()
        self.assertIs(out.state, TransferState.CANCELLED)
        self.assertEqual(out.reason, "DISCONNECTED")
        self.assertTrue(self.wait_for(lambda: not self.relay.registry._active))
        bob.thread.join(timeout=2)
        self.assertEqual(list((Path(self.work.name) / "bob").iterdir()), [])  # no .part left
        # The registry released everything: alice can immediately start another transfer.
        carol = self.peer("carol")
        again, _ = alice.send_file("carol", b"ok")
        self.assertIs(again.state, TransferState.DONE)
        self.assertEqual(len(carol.results), 1)

    def test_rejected_offer_reaches_sender(self) -> None:
        alice = self.peer("alice")
        bob = self.peer("bob", auto_accept=False)
        tid = str(uuid.uuid4())
        out = OutgoingTransfer(tid, 5)
        alice.outgoing[tid] = out
        alice._send(
            "FILE_OFFER",
            transfer_id=tid,
            to="bob",
            name="x",
            size=5,
            sha256=hashlib.sha256(b"hello").hexdigest(),
        )
        time.sleep(0.2)
        bob._send("FILE_REJECT", transfer_id=tid, reason="no thanks")
        self.assertTrue(self.wait_for(lambda: out.finished))
        self.assertEqual((out.state, out.reason), (TransferState.CANCELLED, "no thanks"))


if __name__ == "__main__":
    unittest.main()
