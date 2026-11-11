# Using the application

Use this guide for the full-screen TUI and plain CLI. One computer runs the group
server; every participant, including the administrator, opens a client. TLS protects
each client-server connection. The server can read relayed content; this is not E2EE.

## Install the application

Use Python 3.11 or newer. Download the group's agreed repository and run commands
from its root (the folder containing `run`, `lan-collab` and `lancollab/`). See
[README installation](../README.md#install) for cloning and virtual-environment setup.
Install dependencies with `python -m pip install -r requirements-dev.txt` using the
same Python that will launch the app. Native Windows TUI users also need
`python -m pip install windows-curses`; the plain CLI does not require curses.

Do not run the server or certificate generator on a client just to connect. Obtain
the **current** `server-public.pem` from the admin or agreed trusted repository.

## Server setup

On the administrator's machine, create an admin once and start the server:

```bash
./run --add-admin admin
./run
```

The first command securely prompts for a password and confirmation, then exits.
The second keeps serving until Ctrl+C. `./run` launches `python -m lancollab.server`;
all server flags pass through. Native Windows uses `.\run.bat`.

First startup automatically creates the default certificate/key pair if both files
are missing, using cryptography or the system OpenSSL command. It copies the public
certificate into this checkout's `lancollab/client/server-public.pem`. Existing
pairs are preserved; partial or mismatched pairs fail. Keep the existing identity
when restarting. See [TLS setup](tls-setup.md) for deliberate certificate renewal.

### Give clients the matching public certificate

Only distribute `certs/direct-trust/server-public.pem`. Each client can place that
public file at `lancollab/client/server-public.pem`, or select it explicitly:

```bash
./lan-collab --host SERVER_IP --trusted-cert "/path/to/server-public.pem"
```

Trust-file selection is `--trusted-cert`, then `LANCOLLAB_TRUSTED_CERT`, then the
bundled public certificate. The expected TLS name stays `lan-collab.local` when
connecting by LAN or VPN IP. The app does not fetch a new certificate from GitHub
at login; it uses the local copy.

When distributing through GitHub, the admin must publish the updated **public**
bundle and members must download it. A changed file in the server's working tree
is not yet a change in someone else's clone. Keep `server-secret.pem`, the account
database and saved login tokens private. Compare fingerprints using the
[troubleshooting steps](#troubleshooting) if a client rejects the server.

### Server options

| Option | Meaning |
| --- | --- |
| `--port 40000` | TCP/TLS service port |
| `--udp-port 40001` | Discovery and UDP probe port |
| `--max-clients 32` | Maximum simultaneous connections, including handshakes |
| `--db accounts.db` | SQLite accounts, server tokens and offline private-message queue |
| `--add-admin USER` | Create an active admin account and exit |
| `--change-password USER` | Prompt for a new account password and exit |
| `--cert PATH --key PATH` | Use an existing certificate and matching private key |
| `--auto-approve` | Optional onboarding mode described [below](#optional-automatic-onboarding) |

Relative paths are based on the working directory. Use the same database path for
admin provisioning and server startup. For example:

```bash
./run --db data/group.db --add-admin admin
./run --db data/group.db --port 41000 --udp-port 41001
```

Clients must use matching port options. The default requires manual approval:
new accounts stay pending until an admin approves them from their client.

## Choose the server address

| Where the server runs | Client command |
| --- | --- |
| Same environment as the client | `./lan-collab --host 127.0.0.1` |
| Same reachable LAN | `./lan-collab` to discover, or `./lan-collab --host SERVER_LAN_IP` |
| Reachable over an existing Tailscale network | `./lan-collab --host SERVER_TAILSCALE_IP` |
| Custom ports | `./lan-collab --host SERVER_IP --port 41000 --udp-port 41001` |

Replace uppercase placeholders with actual addresses. `--host` skips discovery.
`127.0.0.1` refers to the client's own environment, not another computer. With WSL,
use an address that reaches the environment running the server; do not assume the
Windows host and WSL have interchangeable addresses. Check host firewall/routing
for TCP 40000 and UDP 40001 (or your configured ports).

For an existing Tailscale setup, run `tailscale status` to map peer addresses to
device names, and `tailscale ip -4` on the server to find its address. Use an
explicit `--host` for this connection. A device name identifies a node, not the
person or process making a connection. Tailscale access policy and host firewall
rules must allow the service, and the client still needs the correct public TLS
certificate. See Tailscale's [connection guide](https://tailscale.com/docs/how-to/connect-to-devices)
and [connectivity troubleshooting](https://tailscale.com/docs/reference/troubleshooting/connectivity/connect-device-failure).
Tailscale is optional; the offline LAN workflow does not require it.

## Connect and join

```bash
./lan-collab
# Or use an explicit IP if UDP broadcast is blocked:
./lan-collab --host SERVER_IP
# Without the TUI (plain line-oriented CLI):
python -m lancollab.client --discover
python -m lancollab.client --host SERVER_IP
```

On Windows, the full-screen launcher requires curses support:

```powershell
python -m pip install windows-curses
.\lan-collab.bat --host 127.0.0.1
# Plain CLI without curses (the launcher always enables the TUI):
python -m lancollab.client --host 127.0.0.1
```

Both interfaces provide the same network features. Choose
Login or Sign up in the initial prompt. Invalid usernames, short signup passwords and
mismatched password confirmation ask for correction before connecting. A successful
signup logs in automatically with the same credentials. Wrong login credentials,
rejected registration and accounts waiting for approval return to the login/signup
menu without restarting the app. After an admin approves your account, choose Login
again. EOF or Ctrl+C exits the prompt. New accounts require admin approval via
`/pending` and `/approve USER` unless the server runs `--auto-approve`.
`/reject-user USER` rejects a pending request.
The password is sent inside verified TLS and stored as a salted scrypt hash. Admin review
shows account names and creation times, not passwords.

The administrator also opens a client and logs in using the provisioned admin
account. The server terminal does not have to pause for approvals. In the admin's
client, use:

```text
/pending
/approve alice
/reject-user bob
```

Replace the example names with pending usernames. Ordinary members cannot approve
accounts. Background pending-list refresh is quiet; an existing user's login or
Remember Me does not create a new signup request.

The TUI starts with a full-screen login/signup form. Click the tabs or press **F2**
to switch modes. **Tab / Shift+Tab / Up / Down** moves focus; Enter advances to the
next field and submits from the button. Passwords are masked. Click the Remember me
checkbox or focus it and press Space. **Esc / Ctrl+C** exits. Validation and rejected
login messages appear inside the form; the username stays filled but passwords are
cleared after a server rejection. The form needs at least **48 columns × 22 rows**;
resize to continue. The plain CLI retains line-oriented prompts.

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
| `/msg USER TEXT` | Private message; queued for an existing active account if offline |
| `/send USER PATH` | Offer a file; quote paths containing spaces |
| `/accept [ID] [PATH]`, `/reject ID`, `/cancel ID` | Consent, decline or cancel a transfer |
| `/history` | Browse your saved login sessions; select one to read its full conversation |
| `/history show ID` | Display every saved message from the selected session (plain console or TUI) |
| `/history USER`, `/history all` | Last 50 messages for a peer in this login, or across your sessions |
| `/pending`, `/approve USER`, `/reject-user USER` | Admin account approval |
| `/net` | UDP RTT and probe-loss measurement |
| `/help`, `/quit`, `/logout` | Open the help screen, close app, or forget login |

The TUI opens with an original ASCII welcome logo and uses a colored conversation area,
connection status, and an online-users sidebar on wide terminals. Color is optional:
set `NO_COLOR=1` to keep a monochrome display. Redirected startup output has no banner
or ANSI color sequences.

- Type `/` to browse commands with descriptions; admin commands appear for admins.
- Use Up/Down to choose a suggestion and Tab to complete it. Enter completes a partial
  suggestion; Enter on a complete command executes it. Clicking a suggestion completes it.
- `/msg`, `/send`, and `/history` also suggest online usernames for their first argument.
- Plain text sends global chat outside Direct. On a Direct conversation page, plain
  text goes only to the selected peer; the Direct inbox never sends to global chat.
- Left/Right, Home/End, Backspace and Delete edit the input. Ctrl+U clears it, Ctrl+W
  deletes the word before the cursor.
- Up/Down recall submitted input when the suggestion menu is closed; Esc dismisses the menu.
- PageUp/PageDown or the mouse wheel scroll the conversation. While scrolled up, the
  header shows how many new messages arrived (`Scrollback · 3 new ↓`).
- Right-click (or left-click) another online user to open **Direct message / Send file**.
  Your own account is hidden from the online interaction list and People menu.
  Choose with a click or Up/Down + Enter; Esc closes the menu. Direct message opens
  that user's conversation page. Send file opens the native file dialog:
  selecting a file sends an offer to that user, while Cancel sends nothing. The
  receiver still has to accept. File dialog errors leave chat usable; `/send USER PATH`
  remains available when tkinter or a desktop display is unavailable.
  Native dialogs temporarily release curses; closing, cancelling or failing a dialog
  restores terminal keyboard/mouse input and redraws the workspace.
  **F7** opens the People menu when the terminal captures right-clicks or no mouse is available.
- Incoming offers show **Accept / Reject** in the right **ACTIVE TRANSFERS** panel.
  Accept opens a folder picker: choose a directory to accept and save this file there;
  Cancel leaves the offer pending. Each file can use a different folder without changing
  the default. With the sidebar hidden or a narrow terminal, the same buttons appear in
  the bottom dock. `/accept ID` and F6 retain the default download folder.
  Admin pending accounts show **Approve / Reject** controls; they still require admin
  permission. Slash commands remain available on narrow terminals.
- Chat lines are colored by sender (your own messages are bold); timestamps are dimmed.
- `F1` or `/help` opens the help screen — a separate full-screen view, not lines in
  the chat. Scroll it with `↑↓` / `PgUp` / `PgDn`; `Ctrl+C` or `Esc` returns to the
  chat. `Ctrl+C` in the chat still exits.

The layout adapts when resized; very small terminals show a resize hint. The normal CLI
remains available without `--tui`.

### Direct conversations

Select **3:Direct** to browse people you have exchanged private messages with, most
recent first. Click a conversation or select it with Up/Down and Enter to open its
message page. Type and Enter to send directly to that peer; no `/msg` prefix is needed.
Use the Back control or Esc to return to the conversation list. To start with someone
new, choose Direct message from their online-user menu (F7 is the keyboard route).

The page combines private messages across your saved login sessions, including
messages from offline peers; it never mixes global chat into a conversation. History
is scoped to the logged-in account and trusted server. With `--no-history`, only this
session's private messages are available. Slash commands remain usable; switching away
from Direct restores the normal global composer. Offline private messages are queued
by the server for delivery when the recipient next logs in.

## Send and receive files

Alice offers a file to Bob:

```text
/send bob "notes/chapter 3.pdf"
```

Bob sees the incoming offer and chooses **Accept** to open the folder picker, or
uses the offer's ID (a unique short prefix is enough):

```text
/accept 4b44b161 "~/Downloads/Study notes"
/transfers
```

The ID is an example, not a fixed value. `/accept` alone accepts the only pending
offer into the default directory. `/reject [ID]` declines an offer; `/cancel [ID]`
stops a transfer. Files may be at most 50 MiB. A picker requires tkinter and a
working desktop display; slash commands work when the picker is unavailable.

Files accepted via `/accept` or F6 are saved in `downloads/` (change with
`--downloads-dir`). To choose a folder for just this file, use
`/accept 123e4567 "~/Documents/Study notes"`, or
`/accept --dir "~/Downloads"` when only one offer is pending. The explicit `--dir PATH` form
also works after an ID. Paths may contain spaces; Windows backslashes are preserved. Under WSL use Linux paths such as
`/mnt/c/Users/YOUR_NAME/Downloads`. On native Windows, use a path such as
`C:\Users\YOUR_NAME\Downloads`. The folder is created if necessary, and the
completion notice shows the saved path. This does not change the default folder;
the Accept button uses the folder chosen in its dialog. `/files`
lists the default downloads directory, not arbitrary chosen folders. The receiver validates
size, order and SHA-256 before committing a unique destination name. The sender reports
success only after the receiver's final success result. Offline recipients cannot receive
files (both sides must be online for a transfer); private messages to an offline user are
stored on the server for their next login. The offline private-message queue holds
up to 100 messages per recipient for up to 24 hours; older messages are dropped
when full. It is not a permanent chat archive, and delivery is not guaranteed if
the server fails while handing off queued messages. Global chat is live only.

## Local history

Chat history is enabled by default in `history.sqlite3` beside the token store:
`~/.config/lan-collab/` on Linux (or `$XDG_CONFIG_HOME/lan-collab`), and
`%APPDATA%/lan-collab/` on Windows. Use `--history-file PATH` to choose a location, or
`--no-history` to disable recording. Logout leaves history intact.

Only incoming chat and acknowledged outgoing chat are saved. An outgoing entry means
server-queued, not proof the recipient read it. Passwords, tokens and file contents are
not stored in this database. History is separated by trusted certificate identity,
server name/port and account; it is not synchronized across devices. Certificate replacement
starts a separate history namespace. Each successful login (including Remember Me) starts
a new session with its login time. `/quit`, `/logout`, and normal disconnects record its
end time. Messages are saved as they arrive, so an abrupt exit keeps saved messages even
if the end time cannot be recorded. Sessions with no messages are also listed.

`/history` opens a session picker in the TUI, newest first. Use Up/Down and Enter to
view a session, then Up/Down or Page Up/Page Down to scroll. Esc or Ctrl+C returns to
the list, and again to live chat. The viewer is read only: opening a session never
resumes it or sends messages. It includes **all incoming global and private chat from
every sender**, plus your acknowledged outgoing messages, in chronological order,
without a 50-message limit. Other accounts' sessions are excluded.

In the plain console, `/history` lists session IDs; `/history show ID` displays the
chosen conversation. Dates use local time, for example `11/11/2026 10:24:00`.
Existing archives remain readable; older sessions use their first saved message time
because their login time was not recorded. `/history show legacy` opens messages saved
before session IDs existed. `/history USER` remains an optional current-session peer
filter, and `/history all` displays the last 50 messages across sessions.

The local database is not encrypted at rest; protect
the OS account and disk. Files are created with restrictive permissions on POSIX systems.

## Dashboard and convenience commands

The TUI header shows your account/role, verified TLS connection, online count, TCP and UDP
round-trip times, and last DM reply target. RTT probes run only when you use `/ping`, `/net`, or F4; the header shows the last
measurement. A dash means no measurement yet. Server liveness keepalives remain silent. At 76 columns or wider, the right panel
shows online users, active transfers and (for admins) pending accounts. F2 toggles it.
Progress updates replace the existing bar instead of adding ACK notices to chat.
The plain interactive CLI also updates a single line; redirected output contains
only lifecycle notices (use `/transfers` for an explicit progress snapshot).
The file dock shows incoming offers and receiver-confirmed outgoing progress; 100% is
not success until receiver verification completes.

| Command | Meaning / example |
| --- | --- |
| `@bob Hello` | Private-message shorthand for `/msg bob Hello` |
| `/reply Thanks` | Reply to the last peer who DMed you or whom you last sent a DM |
| `/accept [ID] [PATH]`, `/reject [ID]`, `/cancel [ID]` | Use a full UUID, unique short prefix, or omit it when exactly one eligible transfer exists |
| `/transfers` | Active transfers with IDs, progress bars, byte counts and status |
| `/files` | Saved files and sizes; temporary downloads are excluded |
| `/search chapter 3` | Literal text/peer search across your local history sessions; last 50 matches |
| `/ping` | Send a TCP/TLS probe and show its RTT when PONG arrives |
| `/whoami` | Account, role, server and downloads location |
| `/clear` | Clear TUI scrollback; keep files and SQLite history |
| `/filter all`, `/filter global`, `/filter dm`, `/filter files` | Select the visible message category |

Tab completes `@username`, pending usernames after `/approve` and `/reject-user`, and
transfer short IDs after file actions. Ambiguous IDs are rejected; enter more characters.
Category tabs can be clicked or selected with Alt+1/2/3/4, so ordinary numeric chat input
still works. F3 cycles filters. F1 opens help; F4 probes TCP and UDP; F5 lists transfers;
F6 accepts the sole pending offer (asks for an ID through an error if there are several).
Ctrl+W deletes the previous word, Ctrl+K deletes to the end, and Ctrl+L clears the display.
Chat colors distinguish senders; file, admin and error notices also have distinct styles.

`/reply TEXT` uses the last DM peer shown in the header; `/msg USER TEXT` explicitly
chooses a recipient. `/help` includes a display-filter
guide: All includes every notice, Global shows group chat, Direct opens private
conversations, and Files shows transfer notices. A selected Direct conversation changes
the plain-text recipient; the other tabs use global chat.

## Optional automatic onboarding

```bash
python -m lancollab.server --auto-approve
# or ./run --auto-approve
```

Manual approval remains the default. With this option, registration creates active accounts;
a first password login for an unknown username also creates an account (minimum eight-character
password). The first account in an **empty database** becomes admin; subsequent accounts are
members. Existing pending users activate only after a correct password login. Incorrect
passwords and rejected accounts are never auto-approved. On a fresh server, the intended
administrator should be the first person to sign up or log in.

## Troubleshooting

### TLS rejects the server (`TLSV1_ALERT_UNKNOWN_CA`)

This server log means the connecting peer rejected the server's certificate. It
does not mean you need to buy a CA certificate. Read the failing client's error;
it prints its trusted certificate path and expected server name.

Compare the fingerprints on the two machines:

```bash
# On the server (substitute the --cert path if customized)
openssl x509 -in certs/direct-trust/server-public.pem -noout -fingerprint -sha256

# On the client (substitute its selected --trusted-cert/environment path if set)
openssl x509 -in lancollab/client/server-public.pem -noout -fingerprint -sha256
```

If they differ, obtain the intended server's current public certificate from the
agreed trusted source and replace the client's copy, or pass `--trusted-cert PATH`.
Restart the client. A server certificate edited on disk requires a server restart
to take effect. Never share `server-secret.pem` or disable verification.

If fingerprints match, check the client's full error, certificate dates, system
clock and expected name (`lan-collab.local`). See [TLS troubleshooting](tls-setup.md#diagnosing-rejected-connections).
Repeated failures from different source ports are separate connection attempts.
If the source is a Tailscale peer, `tailscale status` helps identify the device;
check that device for a client or test script repeatedly connecting.

### Other common problems

| Symptom | What to do |
| --- | --- |
| `No UDP discovery reply` | Start the server; try `--host SERVER_IP`; check UDP port, firewall and LAN routing |
| Connection refused or timeout | Check address, TCP port, server process and route; use the server's reachable address, not another machine's localhost |
| `ACCOUNT_PENDING` | Ask an admin to `/approve USER`, then log in again |
| `No matching transfer` or ambiguous ID | Run `/transfers`; use a current ID or a longer unique prefix |
| Cannot save into a folder | Check the path and write permissions; under WSL use `/mnt/c/...`; retry the pending offer with another folder |
| Picker unavailable | Use `/send USER PATH` or `/accept ID PATH`; native dialogs need tkinter and a desktop display |
| No curses support | Use `python -m lancollab.client --host SERVER_IP`; native Windows TUI needs `windows-curses` |
| Package installed but Python cannot import it | Use `python -m pip install -r requirements-dev.txt` from the active environment |
| `No module named lancollab` | Run from the project root using `python -m lancollab.client`; run tests with `python -m unittest discover -s tests -p "test_*.py"` |

`/ping` checks TCP/TLS latency; `/net` checks UDP; F4 runs both. These are manual
checks. A failed UDP probe does not by itself mean the TCP chat connection is broken.
