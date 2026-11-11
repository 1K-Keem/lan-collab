# Testing guide — LAN Collab

How to download the repo and test the app yourself, from a clean machine.
No extra packages needed: the app is Python standard library only.

## 0. What you need

- **Python 3.11 or newer** — check with `python3 --version` (`python --version`
  on Windows). No `pip install` step: there are no dependencies.
- **Git.**
- Server and clients on the **same network** for auto-discovery. Testing
  everything on one machine works too (localhost).
- On **Windows**, stock Python has no `curses`, so the full-screen TUI does
  not run — use **WSL** for the TUI, or the plain line-oriented CLI. The
  `.bat` launchers below are for Windows; `./run` / `./lan-collab` are for
  Linux, macOS and WSL.

## 1. Get the code

```bash
git clone -b transport https://github.com/1K-Keem/lan-collab.git
cd lan-collab
```

## 2. Add the server's certificate (client trust)

The client trusts **exactly one** certificate — the server's public
certificate — and never falls back to system trust roots. Before a client can
connect, it must have the **matching** `server-public.pem`. There are two
cases:

**A. Testing against the team's server (the usual case).** Get the server's
public certificate from the server operator (through the trusted channel —
the repo). Then point the client at it, one of these three ways:

```bash
# 1. Replace the bundled copy (simplest; it ships with the repo):
cp path/to/server-public.pem lancollab/client/server-public.pem

# 2. Or pass it on the command line (no file change needed):
./lan-collab --host SERVER_IP --trusted-cert path/to/server-public.pem

# 3. Or set the environment variable:
export LANCOLLAB_TRUSTED_CERT=path/to/server-public.pem
./lan-collab --host SERVER_IP
```

Priority is `--trusted-cert`, then `LANCOLLAB_TRUSTED_CERT`, then the bundled
`lancollab/client/server-public.pem`. If the server operator regenerated the
server identity, you need the **new** public certificate — the old one stops
working. **Never ask for or accept `server-secret.pem`**: the private key
stays on the server machine and is never distributed.

**B. Setting up your own server on a new machine (local self-test).** Create
a fresh pair for that machine — never copy another server's private key onto
it; every server has its own identity:

```bash
python -m lancollab.common.devcerts --out certs/direct-trust --bundle-cert
```

This creates `certs/direct-trust/server-public.pem` +
`certs/direct-trust/server-secret.pem` and copies the public certificate into
`lancollab/client/server-public.pem`, so clients on this machine trust this
server. (`./run` would also auto-generate a pair on first start if the
default cert paths are missing, but creating it explicitly is the documented
flow — and it is required if you pass custom `--cert` / `--key` paths, where
no auto-generation happens.)

## 3. Create the first admin

```bash
./run --add-admin admin
# Windows: run.bat --add-admin admin
```

It asks for a password (twice) and exits. By default the server never creates
an admin by itself and never auto-approves accounts — the first admin is
always created this way, by hand. (There is an opt-in `./run --auto-approve`
flag for throwaway local test setups: it activates new accounts on signup and
makes the first account in an empty database the admin. Do not use it on a
shared server.)

## 4. Start the server

```bash
./run
# Windows: run.bat
```

Press `Ctrl+C` to stop the server. Server flags you may need:
`./run --port 40000 --udp-port 40001 --db accounts.db`. The server stores
accounts in `accounts.db` next to where you start it.

## 5. Connect the first client

In a second terminal:

```bash
./lan-collab
# Windows: lan-collab.bat
```

It discovers the server on the LAN and opens the full-screen TUI. If
discovery finds nothing, connect by IP instead:

```bash
./lan-collab --host 127.0.0.1   # same machine
./lan-collab --host 192.168.1.10  # server on the LAN
```

Log in as `admin` with the password from step 3. When asked about Remember Me,
answer `y` to save the login token on this machine. (No TUI? Run the plain
CLI: `python -m lancollab.client --discover`, same login flow.)

## 6. Test the flows

Do these with two accounts: `admin` (from step 2) and a second one you sign
up for, e.g. `bob`.

| What to test | How | Expected |
|---|---|---|
| Sign up + approval | In the client, choose **Sign up** as `bob`, then try to log in as `bob` | "Registered. Waiting for admin approval."; login fails with `ACCOUNT_PENDING` |
| Approve | As `admin`: `/pending`, then `/approve bob` | `/pending` lists `bob`; after approval `bob` can log in |
| Global chat | As `bob`: `/all hello` (or just type `hello` in the TUI) | `admin` sees `[CHAT_GLOBAL] bob: hello` |
| Private chat | As `bob`: `/msg admin hi just for you`; as `admin`: `/r got it` | Private message delivered; `/r` replies to the last DM peer |
| Offline queue | `bob` quits (`/quit`); as `admin`: `/msg bob you missed this` | Admin sees `[stored — bob is offline]`; when `bob` logs back in, the message arrives tagged `[sent while you were offline]` |
| Files | As `admin`: `/send bob path/to/file` (≤ 50 MiB); as `bob`: `/accept ID` | File lands in `downloads/bob/`; SHA-256 verified. Files need **both sides online** — no offline file queue. `/files` lists only the logged-in account's folder |
| History | `/history`, `/history all`, `/history bob` | Current session only for `/history`; all sessions grouped for `/history all` |
| Help screen | Press `F1` or type `/help` | Full-screen help opens; `Ctrl+C` or `Esc` returns to chat; `Ctrl+C` in chat exits |
| Remember Me | `/quit`, relaunch `./lan-collab` | Auto-logs in without a password |
| Log out | `/logout`, relaunch `./lan-collab` | Asks for the password again (token revoked) |
| Presence | `/users` | Lists online users with IP/port; quitting shows the user offline |

## 7. Run the automated tests

```bash
python3 -m unittest discover -s tests -t .
```

273 tests; all should pass. Optional, with `ruff` installed
(`pip install ruff`):

```bash
ruff check lancollab tests
ruff format --check lancollab tests
```

## 8. Clean up

- Stop the server with `Ctrl+C`; leave clients with `/quit`.
- To start from scratch: delete the server's `accounts.db` (and
  `accounts.db-wal` / `-shm` if present) and the client's
  `~/.config/lan-collab/` (saved tokens + chat history).

## Troubleshooting

- `Use --host or --discover` — the client needs one of the two flags.
- Discovery finds nothing — same LAN? Firewall blocking UDP 40001? Use
  `--host SERVER_IP`.
- TLS / certificate error, or `No trusted server certificate` — the client
  does not have the server's matching public certificate. Go back to step 2:
  get the current `server-public.pem` from the server operator and either
  replace the bundled copy or pass `--trusted-cert path/to/server-public.pem`.
  If you run your own server, delete `certs/direct-trust/` and restart
  `./run` to regenerate the pair (this also re-bundles the public cert).
- Port already in use — start the server with different ports, e.g.
  `./run --port 40099 --udp-port 40098`, and match them on the client
  (`./lan-collab --host 127.0.0.1 --port 40099 --udp-port 40098`).
- Something behaves differently from this guide — note the exact command,
  what you saw, and what you expected; that is the bug report.
