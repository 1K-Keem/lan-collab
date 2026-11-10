# Remember Me — token login

How a user stays logged in across restarts without typing their password
every time, and what the server and client actually store.

## The idea in one paragraph

A token is a random 256-bit value, issued once when a user logs in with
their password and asks to be remembered. It is **not** the password and
**not** a hash of the password: stealing it does not reveal the password,
and revoking it does not require changing the password. The token works
for 30 days, then it stops working and the user logs in with a password
again.

## Flows

### First login (password)

1. Client finds the server (UDP discovery or `--host`) and opens a
   verified TLS connection.
2. User picks **Login**, enters username/password, and answers
   `Remember me on this machine? [y/N]`.
3. The client sends `AUTH_REQUEST {username, password, remember_me}`.
4. On success the server replies `AUTH_RESULT {ok, username, role, token?}`.
   The `token` field is present only when `remember_me` was true.
5. The client saves the raw token in its token store (see below) and
   opens the chat screen.

### Later runs (token)

1. New TCP/TLS connection, same as always — TLS is verified every time,
   and authentication still happens on every connection.
2. The client reads its stored token and sends
   `TOKEN_AUTH_REQUEST {token}` — no password is typed or sent.
3. The server replies with the same `AUTH_RESULT` (username + role, no
   new token) and the usual `USER_LIST`. The 30-day expiry is fixed from
   the original issue date; a token login does **not** extend it.

### Closing the app vs logging out

- **Closing the app** (`/quit`, `Ctrl+C`, window close, crash, stdin EOF)
  closes the session and **keeps the token**. Next launch auto-logs in.
  `/quit` sends no `LOGOUT` — the server just sees a disconnect.
- **`/logout`** closes the session **and** revokes the token:
  the client sends `LOGOUT`, the server marks the token revoked, and the
  client deletes its stored copy. Next launch asks for a password again.
  Only `/logout` (and a password change / account rejection) revokes a token.

### When a token stops working

| Situation | What the client sees | What to do |
| --- | --- | --- |
| Token older than 30 days | `AUTH_RESULT {ok: false, error_code: "TOKEN_EXPIRED"}` | Log in with password |
| Token revoked by `/logout` | `TOKEN_REVOKED` | Log in with password |
| Token unknown to the server (wrong DB, deleted row) | `AUTH_FAILED` (deliberately generic) | Log in with password |
| Account pending / rejected / disabled | `ACCOUNT_PENDING` / `ACCOUNT_REJECTED` | Ask an admin |
| Password changed (`--change-password`) or account rejected | all tokens revoked server-side | Log in with the new password |

A dead token is deleted from the client store automatically; the client
falls back to the Login / Sign up prompt.

## What is stored where

**Server — `accounts.db`, `tokens` table (one row per issued token):**

| Column | Contents |
| --- | --- |
| `token_hash` | `SHA-256(raw token)` — the raw token is never stored |
| `username` | account the token belongs to |
| `created_at`, `expires_at` | issue time, issue + 30 days |
| `revoked`, `revoked_at` | revocation flag set by logout / password change / rejection |

**Client — token store (`lancollab/client/token_store.py`):** one
`(username, token)` pair per server host. Backend, in order:

1. the `keyring` package, if installed — the OS credential store
   (Windows Credential Manager / macOS Keychain / Linux Secret Service);
2. otherwise a file with mode `0600` in the user config dir
   (`~/.config/lan-collab/tokens.json` on Linux, `%APPDATA%\lan-collab`
   on Windows). Never a file in the project folder, never world-readable.

The client never stores the password. `keyring` is not a project
dependency (the app is standard-library only); adding it later changes
only `token_store.py`.

## Code map

| Piece | File |
| --- | --- |
| Token issue / verify / revoke | `lancollab/server/tokens.py` (`TokenService`) |
| `TOKEN_AUTH_REQUEST` handling, issue on `remember_me`, revoke on `LOGOUT` | `lancollab/server/application.py` (`CollabServer`) |
| `tokens` table created alongside accounts | `lancollab/server/__main__.py` |
| Revoke on password change / account rejection | `lancollab/server/accounts.py` (`_revoke_tokens`) |
| Client login with token, `issued_token` on `AUTH_RESULT` | `lancollab/client/application.py` (`login_with_token`, `login(..., remember_me=)`) |
| Token storage (keyring, else 0600 file) | `lancollab/client/token_store.py` (`TokenStore`) |
| Auto-login on launch, save on login, delete on `/quit` | `lancollab/client/cli.py`, `lancollab/client/__main__.py` |
| Wire schema | `lancollab/common/message_schema.json`, `lancollab/common/codec.py` |

## Tests

- `tests/test_tokens.py` — issue/verify, wrong token, expiry, revocation,
  token ≠ password hash, password change and account rejection revoke tokens.
- `tests/test_auth_integration.py` — real server over TLS: password login
  with `remember_me` issues a token, token-only login on a new connection
  succeeds, `/quit`-style LOGOUT revokes it, and login without
  `remember_me` issues no token.
- `tests/test_codec.py` — `AUTH_RESULT` with an optional token, and the
  `TOKEN_EXPIRED` / `TOKEN_REVOKED` failure codes.

## Deliberate limits (v1)

- One token per login; a user who logs in with `remember_me` on two
  machines has two independent tokens (revoking one does not revoke the
  other, except a password change, which revokes all of them).
- No "revoke all my sessions" button in the client yet — logout revokes
  only the current session's token; the server CLI can change the password
  to revoke everything.
- Tokens are not bound to an IP address (DHCP changes the client IP),
  only to the verified server identity and the account.
