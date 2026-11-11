"""TokenService unit tests: issue/verify/expiry/revocation."""

import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lancollab.server.accounts import AccountService
from lancollab.server.tokens import TokenService


class TokenServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "accounts.db"
        self.tokens = TokenService(self.db)
        self.addCleanup(self.tokens.close)
        self.accounts = AccountService(self.db)
        self.addCleanup(self.accounts.close)

    def test_issue_verify_and_revoke(self):
        raw = self.tokens.issue("alice")
        self.assertNotEqual(raw, "alice")
        username, error = self.tokens.verify(raw)
        self.assertEqual((username, error), ("alice", None))
        self.assertEqual(self.tokens.verify("wrong-token"), (None, "AUTH_FAILED"))
        self.tokens.revoke_token(raw)
        self.assertEqual(self.tokens.verify(raw), (None, "TOKEN_REVOKED"))

    def test_expired_token(self):
        raw = self.tokens.issue("alice")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tokens SET expires_at = ?", (past,))
        self.assertEqual(self.tokens.verify(raw), (None, "TOKEN_EXPIRED"))

    def test_token_is_not_the_password_hash(self):
        self.accounts.create_account("alice", "password123", status="active")
        raw = self.tokens.issue("alice")
        row = self.accounts._row("alice")
        self.assertNotEqual(raw.encode(), row["password_hash"])
        # A token does not log in as a password, and vice versa.
        self.assertFalse(self.accounts.authenticate("alice", raw)[0])

    def test_change_password_revokes_tokens(self):
        self.accounts.create_account("alice", "password123", status="active")
        raw = self.tokens.issue("alice")
        self.assertEqual(self.tokens.verify(raw)[0], "alice")
        self.accounts.change_password("alice", "new-password-456")
        self.assertEqual(self.tokens.verify(raw), (None, "TOKEN_REVOKED"))

    def test_reject_revokes_tokens(self):
        self.accounts.create_account("alice", "password123")
        raw = self.tokens.issue("alice")
        self.accounts.review("alice", "reject", "admin")
        self.assertEqual(self.tokens.verify(raw), (None, "TOKEN_REVOKED"))


if __name__ == "__main__":
    unittest.main()
