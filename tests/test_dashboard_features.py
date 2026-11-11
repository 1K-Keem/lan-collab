"""Dashboard commands, transfer selection, optional onboarding and provisioning."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import unittest
from unittest.mock import patch

from lancollab.client.application import ClientState
from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.client.history import HistoryStore
from lancollab.client.transfers import TransferError
from lancollab.client.tui import suggestions
from lancollab.common.codec import JsonFrameCodec
from lancollab.common.contracts import Envelope, SessionIdentity
from lancollab.common.devcerts import ensure_server_certs
from lancollab.common.tls import create_server_context, create_client_context
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from tests.fake_transport import FakeTransport


class DashboardCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        a, self.peer = FakeTransport.pair()
        self.output = []
        self.shell = ConsoleSession(a, output=self.output.append, downloads_dir=self.tmp.name)
        self.addCleanup(self.shell.close)
        self.shell.client.state = ClientState.AUTHENTICATED
        self.shell.client.username = "alice"

    def test_at_reply_and_tcp_probe(self):
        self.shell.execute(parse_command("@bob hello"))
        self.assertEqual(self.peer.receive().payload, {"to": "bob", "text": "hello"})
        self.assertEqual(self.shell.client.reply_target, "bob")
        self.shell.client.handle_event(
            Envelope("CHAT_PRIVATE", "event", {"from": "carol", "text": "hi"})
        )
        self.shell.execute(parse_command("/reply thanks"))
        self.assertEqual(self.peer.receive().payload, {"to": "carol", "text": "thanks"})
        self.shell.execute(parse_command("/ping"))
        ping = self.peer.receive()
        self.shell.client.handle_event(Envelope("PONG", ping.request_id, ping.payload))
        self.assertIsNotNone(self.shell.client.last_tcp_rtt)

    def test_no_reply_target_and_transfer_ambiguity(self):
        self.shell.execute(parse_command("/reply hello"))
        self.assertIn("No DM reply target", self.output[-1])
        rows = [dict(id="12345678-one", status="offer"), dict(id="12345678-two", status="offer")]
        with patch.object(self.shell.client.files, "snapshot", return_value=rows):
            with self.assertRaises(TransferError):
                self.shell.client.files.resolve_id("12345678", "accept")
            self.assertEqual(
                self.shell.client.files.resolve_id("12345678-o", "accept"), "12345678-one"
            )
        with patch.object(self.shell.client.files, "snapshot", return_value=rows[:1]):
            self.assertEqual(self.shell.client.files.resolve_id("", "accept"), rows[0]["id"])

    def test_listing_skips_temporary_and_filter_validates(self):
        Path(self.tmp.name, "notes.txt").write_text("hello")
        Path(self.tmp.name, ".lancollab-test.part").write_text("partial")
        self.shell.execute(parse_command("/files"))
        self.assertEqual(self.output, ["notes.txt — 5 bytes"])
        with self.assertRaises(ValueError):
            parse_command("/filter invalid")
        self.shell.execute(parse_command("/filter dm"))
        self.assertEqual(self.shell.display_filter, "dm")
        self.shell.execute(parse_command("/clear"))
        self.assertEqual(self.shell.clear_generation, 1)

    def test_extended_completion_and_search_isolation(self):
        self.assertEqual(suggestions("@b", ["bob"])[0].value, "@bob")
        self.assertEqual(
            suggestions("/approve a", role="admin", pending=["alice"])[0].value, "/approve alice"
        )
        self.assertEqual(suggestions("/approve a", pending=["alice"]), [])
        store = HistoryStore(Path(self.tmp.name) / "history.sqlite3")
        self.addCleanup(store.close)
        store.append(
            "server", "alice", "received", "CHAT_PRIVATE", "bob", "100% done", session="old"
        )
        store.append("other", "alice", "received", "CHAT_PRIVATE", "bob", "100% hidden")
        self.assertEqual(len(store.search("server", "alice", "%")), 1)
        self.assertEqual(len(store.search("server", "alice", "BOB")), 1)


class AutoApproval(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.db"
        self.accounts = AccountService(self.path)
        self.addCleanup(self.accounts.close)
        self.server = CollabServer(self.accounts, auto_approve=True)

    def login(self, name, password="synthetic-password"):
        return self.server.handle_message(
            Envelope("AUTH_REQUEST", "auth", {"username": name, "password": password}),
            SessionIdentity("127.0.0.1", 1234, name),
        )[0].message

    def test_first_login_bootstraps_only_first_user(self):
        self.assertEqual(self.login("alice").payload["role"], "admin")
        self.assertEqual(self.login("bob").payload["role"], "member")
        self.assertEqual(self.accounts.get_account("bob")["status"], "active")

    def test_signup_active_and_default_pending(self):
        request = Envelope(
            "REGISTER_REQUEST", "reg", {"username": "alice", "password": "synthetic-password"}
        )
        session = SessionIdentity("127.0.0.1", 1234, "new")
        reply = self.server.handle_message(request, session)[0].message
        self.assertEqual(reply.payload["status"], "active")
        JsonFrameCodec("server").encode(reply)
        manual = CollabServer(self.accounts)
        request = Envelope(
            "REGISTER_REQUEST", "reg2", {"username": "bob", "password": "synthetic-password"}
        )
        self.assertEqual(
            manual.handle_message(request, session)[0].message.payload["status"], "pending"
        )

    def test_wrong_password_cannot_activate_and_rejected_stays_rejected(self):
        self.accounts.create_account("pending", "synthetic-password")
        self.assertFalse(self.login("pending", "wrong-password").payload["ok"])
        self.assertEqual(self.accounts.get_account("pending")["status"], "pending")
        self.assertTrue(self.login("pending").payload["ok"])
        self.accounts.create_account("rejected", "synthetic-password", status="rejected")
        self.assertFalse(self.login("rejected").payload["ok"])
        self.assertEqual(self.accounts.get_account("rejected")["status"], "rejected")

    def test_two_db_connections_create_exactly_one_admin(self):
        other = AccountService(self.path)
        self.addCleanup(other.close)
        with ThreadPoolExecutor(2) as pool:
            jobs = [
                pool.submit(service.create_account, name, "synthetic-password", bootstrap=True)
                for service, name in [(self.accounts, "alice"), (other, "bob")]
            ]
            for job in jobs:
                job.result()
        roles = [self.accounts.get_account(name)["role"] for name in ["alice", "bob"]]
        self.assertEqual(sorted(roles), ["admin", "member"])


class Provisioning(unittest.TestCase):
    def test_first_launch_and_repeat_preserve_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "client" / "server-public.pem"
            paths = ensure_server_certs(root / "certs", bundled=bundle)
            cert, key = paths.server_cert.read_bytes(), paths.server_key.read_bytes()
            self.assertEqual(bundle.read_bytes(), cert)
            ensure_server_certs(root / "certs", bundled=bundle)
            self.assertEqual(paths.server_key.read_bytes(), key)
            paths.server_key.unlink()
            with self.assertRaises(ValueError):
                ensure_server_certs(root / "certs", bundled=bundle)
            self.assertEqual(paths.server_cert.read_bytes(), cert)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL command unavailable")
    def test_openssl_fallback_creates_loadable_contexts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("importlib.util.find_spec", return_value=None):
                paths = ensure_server_certs(root / "certs", bundled=root / "client.pem")
            create_server_context(str(paths.server_cert), str(paths.server_key))
            create_client_context(str(paths.server_cert))
