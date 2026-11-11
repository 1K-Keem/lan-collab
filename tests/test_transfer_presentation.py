"""Progress stays transient; per-offer directory choices reach the disk receiver."""

import base64
import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from lancollab.client.application import ClientState
from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.client.transfers import Outgoing
from tests.fake_transport import FakeTransport


class TransferPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        transport, peer = FakeTransport.pair()
        self.addCleanup(peer.close)
        self.notices = []
        self.session = ConsoleSession(
            transport, output=self.notices.append, downloads_dir=self.root / "default"
        )
        self.session.client.state = ClientState.AUTHENTICATED
        self.addCleanup(self.session.close)

    def offer(self, content=b"abc"):
        tid = str(uuid4())
        self.session.client.files.on_offer(
            {
                "transfer_id": tid,
                "from": "bob",
                "name": "notes.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        return tid

    def test_destination_grammar(self):
        cases = {
            '/accept abc "C:\\Study notes\\Files"': ("abc", r"C:\Study notes\Files"),
            '/accept abc "~/Study notes"': ("abc", "~/Study notes"),
            "/accept abc ./notes": ("abc", "./notes"),
            '/accept abc --dir "C:\\Study notes\\Files"': ("abc", r"C:\Study notes\Files"),
            "/accept --dir ~/Study notes": ("", "~/Study notes"),
            "/accept abc": ("abc",),
            "/accept": ("",),
        }
        for text, args in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).args, args)
        for text in (
            "/accept --dir",
            '/accept abc --dir ""',
            "/accept abc --unknown",
            '/accept --dir "x',
            '/accept abc ""',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)

    def test_accept_custom_folder_verifies_file_and_preserves_existing_file(self):
        tid = self.offer()
        target = self.root / "Study notes"
        target.mkdir()
        (target / "notes.txt").write_bytes(b"existing")
        self.session.execute(parse_command(f'/accept {tid[:8]} "{target}"'))
        files = self.session.client.files
        self.assertEqual(files.incoming[tid].temp_path.parent, target)
        files.on_chunk(
            {
                "transfer_id": tid,
                "seq": 0,
                "offset": 0,
                "data_b64": base64.b64encode(b"abc").decode(),
            }
        )
        files.on_end({"transfer_id": tid, "total_chunks": 1})
        self.session._drain()
        self.assertEqual((target / "notes.txt").read_bytes(), b"existing")
        self.assertEqual((target / "notes (1).txt").read_bytes(), b"abc")
        self.assertFalse(list(target.glob("*.part")))
        self.assertFalse((self.root / "default").exists())
        self.assertTrue(any(str(target / "notes (1).txt") in notice for notice in self.notices))

    def test_bad_destination_keeps_offer_available_for_retry(self):
        tid = self.offer()
        target = self.root / "a-file"
        target.write_text("occupied")
        self.session.execute(parse_command(f'/accept --dir "{target}"'))
        self.assertIsNone(self.session.client.files.incoming[tid].handle)
        self.assertTrue(any("[error]" in notice for notice in self.notices))
        self.session.execute(parse_command("/accept"))
        self.assertIsNotNone(self.session.client.files.incoming[tid].handle)

    def test_ack_updates_bar_without_transcript_spam_or_premature_success(self):
        files = self.session.client.files
        tid = str(uuid4())
        outgoing = Outgoing(self.root / "notes.txt", 32, "0" * 64, "bob")
        outgoing.started = True
        outgoing.sent_bytes = 32
        outgoing.outstanding.extend(range(1, 33))
        files.outgoing[tid] = outgoing
        stream = io.StringIO()
        self.session.output = print
        with patch("sys.stdout", stream), patch.object(stream, "isatty", return_value=True):
            for offset in range(1, 33):
                files.on_ack({"transfer_id": tid, "next_offset": offset})
                self.session._drain()
            # Many redraws, but no new scrollback lines or success notice.
            self.assertNotIn("\n", stream.getvalue())
            self.assertIn("32/32 B", stream.getvalue())
            self.assertIn("unverified", stream.getvalue())
            self.assertEqual(files.snapshot()[0]["done"], 32)
            self.assertEqual(self.session.client.drain_notices(), [])
            outgoing.end_sent = True
            files.on_terminal("FILE_RESULT", {"transfer_id": tid, "ok": True})
            self.session._drain()
            self.assertEqual(stream.getvalue().count("verified successfully"), 1)
            self.assertEqual(stream.getvalue().count("\n"), 1)
            self.assertEqual(self.session._progress_line, "")
        self.session.output = self.notices.append

    def test_redirected_output_does_not_emit_progress_controls(self):
        stream = io.StringIO()
        self.session.output = print
        with patch("sys.stdout", stream):
            self.session._drain()
        self.assertEqual(stream.getvalue(), "")
        self.session.output = self.notices.append
