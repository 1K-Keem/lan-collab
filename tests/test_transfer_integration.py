"""Transfer primitives exercised through the production authenticated TLS relay."""

from typing import Any
import hashlib
import itertools
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import create_server_context
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.connections import ConnectionServer
from lancollab.common.transfer_core import (
    DiskSink,
    IncomingTransfer,
    OutgoingTransfer,
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


class ProductionRelay:
    """Isolated accounts and ports; no alternate network implementation in tests."""

    def __init__(self, context):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = AccountService(Path(self.tmp.name) / "accounts.db")
        handler = CollabServer(self.accounts)
        self.registry = handler.transfers
        self.server = ConnectionServer(context, port=0, udp_port=0, handler=handler)
        self.port = self.server.port
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def close(self):
        self.server.close()
        self.thread.join(5)
        self.accounts.close()
        self.tmp.cleanup()


class Peer:
    """A client: one reader thread (never blocks on credit) plus caller-driven sender."""

    def __init__(
        self, relay: ProductionRelay, ca: str, name: str, download_dir: str, auto_accept=True
    ):
        relay.accounts.create_account(name, "synthetic-test-only", status="active")
        self.name = name
        self.dir = download_dir
        self.auto_accept = auto_accept
        self.transport = SocketTransport.connect_verified("127.0.0.1", relay.port, ca)
        self.transport.send(
            Envelope("AUTH_REQUEST", rid(), {"username": name, "password": "synthetic-test-only"})
        )
        self.transport.receive(timeout=5)
        self.requests: dict[str, str] = {}
        self.outgoing: dict[str, OutgoingTransfer] = {}
        self.incoming: dict[str, IncomingTransfer] = {}
        self.results: dict[str, Any] = {}
        self.cancel_after_first_chunk = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _send(self, kind: str, **payload) -> None:
        request_id = rid()
        if "transfer_id" in payload:
            self.requests[request_id] = payload["transfer_id"]
        self.transport.send(Envelope(kind, request_id, payload))

    def _read(self) -> None:
        try:
            while True:
                self._handle(self.transport.receive())
        except Exception:  # disconnect/protocol failure abandons the active transfers
            for out in self.outgoing.values():
                out.on_cancel("CONNECTION_LOST")
            for inc in self.incoming.values():
                inc.cancel()  # closes and deletes the partial file

    def _handle(self, m: Envelope) -> None:
        p = m.payload
        tid = p.get("transfer_id")
        if m.type == "PING":
            self.transport.send(Envelope("PONG", m.request_id, {"nonce": p["nonce"]}))
        elif m.type == "FILE_OFFER" and self.auto_accept:
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
        elif m.type == "ERROR":
            # A late error for the cancelled transfer must not cancel a later offer.
            rejected_id = self.requests.get(m.request_id)
            out = self.outgoing.get(rejected_id)
            if out is not None and not out.finished:
                out.on_cancel(p["code"])
        elif m.type == "FILE_CANCEL":
            if tid in self.incoming:
                self.incoming[tid].cancel()
            if tid in self.outgoing:
                self.outgoing[tid].on_cancel(p["reason"])
        elif isinstance(tid, str) and tid in self.outgoing:  # <--- add isinstance check
            out = self.outgoing[tid]
            if out.finished:
                return
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
        self.relay = ProductionRelay(self.ctx)
        self.addCleanup(self.relay.close)
        self.ca = str(self.certs.server_cert)
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
