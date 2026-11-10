# Using the application

## First setup

Use the group's agreed repository and matching public certificate. The server operator
keeps `server-secret.pem` private. On a new server installation only:

```bash
python -m lancollab.common.devcerts --out certs/direct-trust --bundle-cert
python -m lancollab.server --add-admin admin
python -m lancollab.server
```

If certificates already exist, keep them and use the existing setup. Regenerating the
identity requires redistributing the public certificate; see [TLS setup](tls-setup.md).
Do not distribute the private key or the account database.

## Connect and join

```bash
python -m lancollab.client --discover
# Or use an explicit IP if UDP broadcast is blocked:
python -m lancollab.client --host SERVER_IP
# Full-screen interface on Python installations with curses (Linux/macOS/WSL):
python -m lancollab.client --host SERVER_IP --tui
```

On Windows Python without curses, the default CLI supports the same features. Choose
Login or Sign up in the initial prompt. New accounts are pending until an admin logs in
and uses `/pending` and `/approve USER`. `/reject-user USER` rejects a pending request.
The password is sent inside verified TLS and stored as a salted scrypt hash. Admin review
shows account names and creation times, not passwords.

Remember Me stores a revocable token on this machine. Every launch still performs a new
TCP/TLS handshake and server authentication. `/quit` preserves the token; `/logout`
revokes it when connected and deletes the local copy even if disconnected. Network or
certificate errors do not erase a valid saved login. Credential and Remember Me prompts
happen before connecting, so user typing does not consume the authentication deadline.

## Commands

| Command | Action |
| --- | --- |
| `/users`, `/status` | Online users and connection state |
| `/all TEXT` | Global message |
| `/msg USER TEXT` | Private message to an online user |
| `/send USER PATH` | Offer a file; quote paths containing spaces |
| `/accept ID`, `/reject ID`, `/cancel ID` | Consent, decline or cancel a transfer |
| `/history`, `/history USER` | Last 50 locally saved messages, optionally filtered by peer |
| `/pending`, `/approve USER`, `/reject-user USER` | Admin account approval |
| `/net` | UDP RTT and probe-loss measurement |
| `/help`, `/quit`, `/logout` | Help, close app, or forget login |

The TUI uses these same commands, displays online users and keeps a scrollable message
area. PageUp/PageDown scroll. Ctrl+C or Ctrl+D exits; neither revokes Remember Me.

Files are saved in `downloads/` (change with `--downloads-dir`). The receiver validates
size, order and SHA-256 before committing a unique destination name. The sender reports
success only after the receiver's final success result. Offline recipients cannot receive
files or messages; there is no server-side offline mailbox.

## Local history

Chat history is enabled by default in `history.sqlite3` beside the token store:
`~/.config/lan-collab/` on Linux (or `$XDG_CONFIG_HOME/lan-collab`), and
`%APPDATA%/lan-collab/` on Windows. Use `--history-file PATH` to choose a location, or
`--no-history` to disable recording. Logout leaves history intact.

Only incoming chat and acknowledged outgoing chat are saved. An outgoing entry means
server-queued, not proof the recipient read it. Passwords, tokens and file contents are
not stored in this database. History is separated by trusted certificate identity,
server name/port and account; it is not synchronized across devices. Certificate replacement
starts a separate history namespace. The local database is not encrypted at rest; protect
the OS account and disk. Files are created with restrictive permissions on POSIX systems.
