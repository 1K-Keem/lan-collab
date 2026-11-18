import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lancollab.server.__main__ import _manage_users, main
from lancollab.server.accounts import AccountError, AccountService
from lancollab.server.maintenance import database_maintenance_lock
from lancollab.server.queue import MessageQueue
from lancollab.server.tokens import TokenService


class UserManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "accounts.db"
        self.accounts = AccountService(self.path)
        self.addCleanup(self.accounts.close)
        for username, role, status in [
            ("admin", "admin", "active"),
            ("alice", "member", "active"),
            ("bob", "member", "pending"),
            ("carol", "member", "rejected"),
        ]:
            self.accounts.create_account(username, "synthetic-password", role=role, status=status)

    def test_list_includes_every_status_without_secrets(self):
        rows = self.accounts.list_accounts()
        self.assertEqual({r["status"] for r in rows}, {"active", "pending", "rejected"})
        self.assertEqual(set(rows[0]), {"username", "role", "status", "created_at"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(_manage_users(self.path), 0)
        self.assertIn("4 account(s)", output.getvalue())
        self.assertIn("carol", output.getvalue())
        self.assertNotIn("synthetic-password", output.getvalue())

    def test_delete_cleans_tokens_queue_and_groups_but_keeps_others(self):
        tokens = TokenService(self.path)
        queue = MessageQueue(self.path)
        self.addCleanup(tokens.close)
        self.addCleanup(queue.close)
        old_token = tokens.issue("alice")
        kept_token = tokens.issue("admin")
        queue.enqueue("alice", "admin", "CHAT_PRIVATE", {"text": "incoming"})
        queue.enqueue("admin", "alice", "CHAT_PRIVATE", {"text": "outgoing"})
        queue.enqueue("admin", "admin", "CHAT_PRIVATE", {"text": "keep"})
        self.accounts.create_group("owned", ["admin", "alice"], "alice")
        self.accounts.create_group("joined", ["admin", "alice"], "admin")
        self.accounts.delete_account("alice")
        self.assertIsNone(self.accounts.get_account("alice"))
        self.assertEqual(queue.count("alice"), 0)
        self.assertEqual(queue.count("admin"), 1)
        self.assertIsNone(tokens.verify(old_token)[0])
        self.assertEqual(tokens.verify(kept_token)[0], "admin")
        self.assertEqual(
            self.accounts.list_groups("admin"),
            [{"name": "joined", "members": ["admin"], "owner": "admin"}],
        )
        self.accounts.create_account("alice", "different-password", status="active")
        self.assertIsNone(tokens.verify(old_token)[0])

    def test_missing_and_last_admin_leave_data_intact(self):
        for username, code in [("missing", "USER_NOT_FOUND"), ("admin", "LAST_ACTIVE_ADMIN")]:
            with self.assertRaisesRegex(AccountError, code):
                self.accounts.delete_account(username)
        self.assertEqual(len(self.accounts.list_accounts()), 4)
        self.accounts.create_account("admin2", "synthetic-password", role="admin", status="active")
        self.accounts.delete_account("admin")
        self.assertIsNone(self.accounts.get_account("admin"))

    def test_failed_cleanup_rolls_back_every_change(self):
        tokens = TokenService(self.path)
        self.addCleanup(tokens.close)
        token = tokens.issue("alice")
        self.accounts._conn.execute(
            "CREATE TRIGGER reject_delete BEFORE DELETE ON accounts "
            "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
        )
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.accounts.delete_account("alice")
        self.assertIsNotNone(self.accounts.get_account("alice"))
        self.assertEqual(tokens.verify(token)[0], "alice")

    def test_confirmation_cancel_then_confirm(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", return_value="no"):
            self.assertEqual(_manage_users(self.path, "alice"), 0)
        self.assertIsNotNone(self.accounts.get_account("alice"))
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch("builtins.input", return_value="alice"),
        ):
            self.assertEqual(_manage_users(self.path, "alice"), 0)
        self.assertIsNone(self.accounts.get_account("alice"))

    def test_running_server_lock_blocks_deletion_across_processes(self):
        command = [
            sys.executable,
            "-m",
            "lancollab.server",
            "--db",
            str(self.path),
            "--delete-user",
            "alice",
            "--yes",
        ]
        with database_maintenance_lock(self.path):
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Stop its server", result.stdout)
        self.assertIsNotNone(self.accounts.get_account("alice"))
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.accounts.get_account("alice"))

    def test_missing_database_is_not_created(self):
        missing = self.path.parent / "missing.db"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_manage_users(missing), 1)
        self.assertFalse(missing.exists())

    def test_management_actions_are_exclusive(self):
        for args in (["--list-users", "--delete-user", "alice"], ["--yes"]):
            with (
                patch.object(sys, "argv", ["server", *args]),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as result:
                    main()
            self.assertEqual(result.exception.code, 2)
