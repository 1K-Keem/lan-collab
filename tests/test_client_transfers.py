import base64
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from uuid import uuid4

from lancollab.client.transfers import (
    MAX_FILE_BYTES,
    TransferError,
    TransferManager,
    checked_filename,
)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []
        self.notices = []
        self.transfer = TransferManager(
            lambda kind, data: self.sent.append((kind, data)),
            self.root / "downloads",
            notify=self.notices.append,
        )

    def tearDown(self):
        self.transfer.close()
        self.temp.cleanup()

    def offer_payload(self, content=b"hi", name="hello.txt"):
        return {
            "transfer_id": str(uuid4()),
            "from": "alice",
            "name": name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def test_unsafe_names_rejected(self):
        for name in (
            "../a.txt",
            "CON",
            "con.txt",
            "foo/evil",
            "evil\\file",
            "bad.",
            "bad ",
            "bad:name",
            "x\x1b.txt",
            ".",
            "..",
        ):
            with self.subTest(name=name), self.assertRaises(TransferError):
                checked_filename(name)

    def test_offer_hash_and_consent(self):
        source = self.root / "good.bin"
        source.write_bytes(b"a\x00\xff")
        tid = self.transfer.offer(source, "bob")
        kind, payload = self.sent.pop(0)
        self.assertEqual(kind, "FILE_OFFER")
        self.assertEqual(payload["sha256"], hashlib.sha256(b"a\x00\xff").hexdigest())
        self.assertEqual(payload["transfer_id"], tid)
        self.assertEqual(payload["size"], 3)
        self.assertFalse(self.transfer.outgoing[tid].end_sent)

    def test_over_limit(self):
        path = self.root / "large.bin"
        with path.open("wb") as file:
            file.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaises(TransferError):
            self.transfer.offer(path, "bob")

    def test_receiver_end_and_atomic_no_overwrite(self):
        payload = self.offer_payload(b"abc")
        self.transfer.on_offer(payload)
        target = self.root / "downloads" / "hello.txt"
        target.parent.mkdir()
        target.write_bytes(b"old")
        self.transfer.accept(payload["transfer_id"])
        self.assertEqual(self.sent[-1][0], "FILE_ACCEPT")
        self.transfer.on_chunk(
            {
                "transfer_id": payload["transfer_id"],
                "seq": 0,
                "offset": 0,
                "data_b64": base64.b64encode(b"abc").decode(),
            }
        )
        self.assertEqual(
            self.sent[-1], ("FILE_ACK", {"transfer_id": payload["transfer_id"], "next_offset": 3})
        )
        self.transfer.on_end({"transfer_id": payload["transfer_id"], "total_chunks": 1})
        self.assertEqual(self.sent[-1][1]["ok"], True)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual((target.parent / "hello (1).txt").read_bytes(), b"abc")
        self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_empty_file_ack_zero_then_result(self):
        payload = self.offer_payload(b"", name="empty.bin")
        self.transfer.on_offer(payload)
        self.transfer.accept(payload["transfer_id"])
        self.transfer.on_end({"transfer_id": payload["transfer_id"], "total_chunks": 0})
        self.assertEqual([type for type, _ in self.sent][-2:], ["FILE_ACK", "FILE_RESULT"])
        self.assertEqual(self.sent[-2][1]["next_offset"], 0)
        self.assertTrue(self.sent[-1][1]["ok"])
        self.assertEqual((self.root / "downloads" / "empty.bin").stat().st_size, 0)

    def test_hash_mismatch_discards_temp(self):
        payload = self.offer_payload(b"correct")
        self.transfer.on_offer(payload)
        self.transfer.accept(payload["transfer_id"])
        self.transfer.on_chunk(
            {
                "transfer_id": payload["transfer_id"],
                "seq": 0,
                "offset": 0,
                "data_b64": base64.b64encode(b"incorrect"[:7]).decode(),
            }
        )
        self.transfer.on_end({"transfer_id": payload["transfer_id"], "total_chunks": 1})
        self.assertFalse(self.sent[-1][1]["ok"])
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_out_of_order_and_before_consent(self):
        payload = self.offer_payload()
        self.transfer.on_offer(payload)
        with self.assertRaises(TransferError):
            self.transfer.on_chunk(
                {"transfer_id": payload["transfer_id"], "seq": 0, "offset": 0, "data_b64": "YQ=="}
            )
        self.transfer.accept(payload["transfer_id"])
        with self.assertRaises(TransferError):
            self.transfer.on_chunk(
                {"transfer_id": payload["transfer_id"], "seq": 1, "offset": 0, "data_b64": "YQ=="}
            )

    def test_cancel_removes_temp(self):
        payload = self.offer_payload()
        self.transfer.on_offer(payload)
        self.transfer.accept(payload["transfer_id"])
        self.transfer.cancel(payload["transfer_id"])
        self.assertEqual(self.sent[-1][0], "FILE_CANCEL")
        self.assertEqual(list((self.root / "downloads").iterdir()), [])
        self.assertIn(payload["transfer_id"], self.transfer.terminal)

    def test_window_8_and_receiver_ack_credit(self):
        path = self.root / "nine.bin"
        path.write_bytes(b"z" * (9 * 16384))
        tid = self.transfer.offer(path, "bob")
        self.transfer.on_accept({"transfer_id": tid})
        deadline = monotonic() + 2.0
        while len([x for x in self.sent if x[0] == "FILE_CHUNK"]) < 8 and monotonic() < deadline:
            sleep(0.01)
        chunks = [x for x in self.sent if x[0] == "FILE_CHUNK"]
        self.assertEqual(len(chunks), 8)
        self.assertNotIn("FILE_END", [x[0] for x in self.sent])
        self.transfer.on_ack({"transfer_id": tid, "next_offset": 4 * 16384})
        deadline = monotonic() + 2.0
        while "FILE_END" not in [x[0] for x in self.sent] and monotonic() < deadline:
            sleep(0.01)
        chunks = [x for x in self.sent if x[0] == "FILE_CHUNK"]
        self.assertEqual(len(chunks), 9)
        self.assertEqual(self.sent[-1], ("FILE_END", {"transfer_id": tid, "total_chunks": 9}))
        self.assertEqual(self.transfer.outgoing[tid].ack_bytes, 4 * 16384)
        with self.assertRaises(TransferError):
            self.transfer.on_ack({"transfer_id": tid, "next_offset": 4 * 16384 + 1})
        with self.assertRaises(TransferError):
            self.transfer.on_terminal("FILE_RESULT", {"transfer_id": tid, "ok": True})
        self.transfer.on_ack({"transfer_id": tid, "next_offset": 9 * 16384})
        self.transfer.on_terminal("FILE_RESULT", {"transfer_id": tid, "ok": True})
        self.assertIn(tid, self.transfer.terminal)
        self.assertTrue(any("verified" in x for x in self.notices))


if __name__ == "__main__":
    unittest.main()
