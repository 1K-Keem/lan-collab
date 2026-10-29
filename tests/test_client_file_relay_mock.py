"""Deterministic simulated file relay, NOT a real TCP/TLS test."""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from lancollab.client.transfers import TransferManager


class MockFileRelayTests(unittest.TestCase):
    def test_sizes_and_ack_finish(self):
        for length in (0, 1, 3, 4, 8, 9, 9 * 16384 + 7):
            with self.subTest(length=length), TemporaryDirectory() as directory:
                base = Path(directory)
                data = (b"\x00\x80\xffDATA" * ((length + 6) // 7))[:length]
                source = base / "binary.dat"
                source.write_bytes(data)
                history = []
                send_state = []

                def alice_send(kind, payload):
                    history.append(("alice", kind, payload))
                    if kind == "FILE_OFFER":
                        bob.handle_event(
                            "FILE_OFFER",
                            {
                                "transfer_id": payload["transfer_id"],
                                "from": "alice",
                                "name": payload["name"],
                                "size": payload["size"],
                                "sha256": payload["sha256"],
                            },
                        )
                    else:
                        bob.handle_event(kind, payload)

                def bob_send(kind, payload):
                    history.append(("bob", kind, payload))
                    alice.handle_event(kind, payload)

                alice = TransferManager(alice_send, base / "alice", notify=send_state.append)
                bob = TransferManager(bob_send, base / "bob")
                try:
                    transfer_id = alice.offer(source, "bob")
                    bob.accept(transfer_id)
                    deadline = monotonic() + 5.0
                    while transfer_id not in alice.terminal and monotonic() < deadline:
                        sleep(0.01)
                    self.assertIn(transfer_id, alice.terminal)
                    self.assertEqual((base / "bob" / "binary.dat").read_bytes(), data)
                    self.assertEqual(
                        hashlib.sha256(data).hexdigest(),
                        hashlib.sha256((base / "bob" / "binary.dat").read_bytes()).hexdigest(),
                    )
                    self.assertTrue(any("verified successfully" in n for n in send_state))
                    types = [kind for _, kind, _ in history]
                    self.assertLess(types.index("FILE_END"), types.index("FILE_RESULT"))
                    self.assertLess(types.index("FILE_ACK"), types.index("FILE_RESULT"))
                finally:
                    alice.close()
                    bob.close()

    def test_cancelled_transfer_cannot_restart(self):
        with TemporaryDirectory() as root:
            events = []
            mgr = TransferManager(lambda kind, data: events.append((kind, data)), root)
            src = Path(root) / "data.txt"
            src.write_text("test")
            tid = mgr.offer(src, "bob")
            mgr.cancel(tid)
            mgr.handle_event("FILE_ACCEPT", {"transfer_id": tid})
            self.assertEqual([x[0] for x in events], ["FILE_OFFER", "FILE_CANCEL"])
            mgr.close()


if __name__ == "__main__":
    unittest.main()
