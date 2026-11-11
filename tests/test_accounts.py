import tempfile
import unittest
from pathlib import Path

from lancollab.server.accounts import AccountError, AccountService


class AccountServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = AccountService(Path(self.tmp.name) / "accounts.db")
        self.addCleanup(self.accounts.close)

    def test_register_pending_then_approve_then_login(self):
        self.accounts.create_account("alice", "password123")
        ok, code, _ = self.accounts.authenticate("alice", "password123")
        self.assertFalse(ok)
        self.assertEqual(code, "ACCOUNT_PENDING")
        self.assertEqual(self.accounts.review("alice", "approve", "admin"), "active")
        ok, code, role = self.accounts.authenticate("alice", "password123")
        self.assertTrue(ok)
        self.assertEqual(role, "member")

    def test_wrong_password_never_reveals_status(self):
        self.accounts.create_account("alice", "password123")
        ok, code, _ = self.accounts.authenticate("alice", "wrong-password")
        self.assertFalse(ok)
        self.assertEqual(code, "AUTH_FAILED")

    def test_duplicate_username_and_short_password(self):
        self.accounts.create_account("alice", "password123")
        with self.assertRaises(AccountError) as ctx:
            self.accounts.create_account("alice", "password123")
        self.assertEqual(ctx.exception.code, "USERNAME_TAKEN")
        with self.assertRaises(AccountError) as ctx:
            self.accounts.create_account("bob", "short")
        self.assertEqual(ctx.exception.code, "INVALID_PASSWORD")

    def test_change_password(self):
        self.accounts.create_account("alice", "password123", status="active")
        self.accounts.change_password("alice", "new-password-456")
        ok, code, _ = self.accounts.authenticate("alice", "password123")
        self.assertFalse(ok)
        self.assertEqual(code, "AUTH_FAILED")
        ok, _, role = self.accounts.authenticate("alice", "new-password-456")
        self.assertTrue(ok)
        self.assertEqual(role, "member")

    def test_change_password_unknown_user_and_short_password(self):
        with self.assertRaises(AccountError) as ctx:
            self.accounts.change_password("ghost", "new-password-456")
        self.assertEqual(ctx.exception.code, "USER_NOT_FOUND")
        self.accounts.create_account("alice", "password123", status="active")
        with self.assertRaises(AccountError) as ctx:
            self.accounts.change_password("alice", "short")
        self.assertEqual(ctx.exception.code, "INVALID_PASSWORD")

    def test_reject_and_double_review(self):
        self.accounts.create_account("alice", "password123")
        self.assertEqual(self.accounts.review("alice", "reject", "admin"), "rejected")
        ok, code, _ = self.accounts.authenticate("alice", "password123")
        self.assertEqual(code, "ACCOUNT_REJECTED")
        with self.assertRaises(AccountError) as ctx:
            self.accounts.review("alice", "approve", "admin")
        self.assertEqual(ctx.exception.code, "ACCOUNT_NOT_PENDING")
