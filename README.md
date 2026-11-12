# LAN Collab

A terminal application for students and study groups to chat and share files through
one group server. It works on a reachable local network without internet access
once the application and trusted server certificate are installed.

The server relays messages and files between clients. Connections use verified TLS;
this is client-server encryption, not end-to-end encryption between users.

## What you can do

- Chat with the group or open private conversation pages.
- Sign up and have an admin approve your account; optionally remember your login.
- Send files up to 50 MiB, choose where to save each incoming file, and follow a
  live progress bar. Both users must be online for a file transfer.
- Browse and search chat history saved on your own computer.
- Use a colored full-screen terminal interface with command suggestions, mouse
  menus and a file picker, or use the plain command-line interface.
- Connect by LAN discovery or an explicit server IP. The server allows up to
  32 simultaneous connections by default.

## Install

Use Python 3.11 or newer. Download or clone the repository agreed with your group:

```bash
git clone https://github.com/1K-Keem/lan-collab.git
cd lan-collab
python -m venv .venv
```

Activate the environment and install the setup/test dependencies:

```bash
# Linux, macOS or WSL
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip install windows-curses
```

Run the commands below from the project root. `python -m pip` installs into the
Python environment you are actually using. The network runtime uses Python's
standard library; the requirements file provides certificate-generation and test tools.

## Install the `lancollab` command

With Python 3.11 or newer installed, run this once from the repository folder:

```bash
# Linux, macOS, or WSL
./setup
```

```powershell
# Windows PowerShell or Command Prompt
.\setup.bat
```

Setup installs the app in a private environment and adds its command directory
to your user PATH. It uses pipx, preparing its own setup tools if pipx is missing.
No administrator access or virtual-environment activation is needed. The first
installation needs internet access to download the packaging tools and dependencies.
On Debian/Ubuntu, install `python3-venv` first if Python cannot create virtual environments.

Open a new terminal, then run from any folder:

```text
lancollab
```

This opens the full-screen client at **206.189.94.234:40000**, equivalent to
`python -m lancollab.client --host 206.189.94.234 --port 40000 --tui`.
Windows curses support and the bundled public server certificate are included.
Install in the environment where you will run it: Windows and WSL are separate.

Optional overrides:

```text
lancollab --host 127.0.0.1 --port 40000
lancollab --discover
lancollab --help
```

After updating this repository, run `./setup` again (`.\setup.bat` on Windows).
Remove the installed command with `./setup --uninstall` or `.\setup.bat --uninstall`.
Use `--no-path` if you manage PATH yourself. Uninstall preserves saved login,
history, and downloads. Setup tools and PATH entries may remain for future reinstalls.
An installed copy includes the certificate present at installation time; if the
server certificate changes, reinstall with the updated public certificate or pass
`--trusted-cert PATH`. Downloads still go to `downloads/` in your current folder
unless you pass `--downloads-dir PATH`. Saved login and history use your normal
per-user configuration folder.

For development inside an already active virtual environment, `python -m pip install -e .`
also adds `lancollab`, available while that environment is active.

## Start the group server — administrator

Create the first admin once, then leave the server running:

```bash
python -m lancollab.server --add-admin admin
python -m lancollab.server
```

Both commands prompt or run in the terminal; the admin password is not a command
argument. These server commands work on Windows, Linux, macOS, and WSL.

On first startup, the server creates `certs/direct-trust/server-public.pem` and
`server-secret.pem` if both are missing, and copies the public certificate to
`lancollab/client/server-public.pem`. Existing server identities are preserved.

**Distribute the current public certificate to the group before clients connect.**
Publish the matching `lancollab/client/server-public.pem` in the agreed repository
or give it to members through another trusted channel. A local file change is not
available to other clones until it is published and downloaded. Keep
`server-secret.pem` and `accounts.db` on the server.

The default ports are TCP **40000** and UDP **40001**. See the
[user guide](docs/client-guide.md#server-setup) for custom ports, database location
and optional automatic account approval.

## Open the client — everyone, including admins

After setup, open a new terminal:

```bash
lancollab                           # Open the configured server and TUI
lancollab --discover                # Discover a server on the LAN
lancollab --host SERVER_IP           # Explicit server address
lancollab --host 127.0.0.1            # Client and server in the same environment
```

Replace `SERVER_IP` with the address supplied by your administrator. On native
Windows use the same installed command. From the source folder, you can also use the plain CLI:

```bash
python -m lancollab.client --host SERVER_IP
```

1. The administrator chooses **Login** using the account created above.
2. New members choose **Sign up** and set their own passwords.
3. The admin runs `/pending`, then `/approve alice` in their client.
4. The approved member chooses **Login** again. **Remember me** skips the login
   form on later launches while still verifying the server through TLS.

Default onboarding requires approval. An admin uses the same client interface as
other members, with extra account-review commands; the server process stays running.

## Chat and share files

| Task | Command or action |
| --- | --- |
| Find commands | Type `/` for suggestions; `/help` or F1 opens detailed help |
| Group chat | `/all Hello everyone` (or plain text in the TUI outside Direct) |
| Private chat | `/msg bob Hello`, `@bob Hello`, or open Bob's Direct page |
| Send a file | `/send bob "notes/chapter 3.pdf"` |
| Choose a save folder | `/accept 4b44b161 "~/Downloads/Study notes"` |
| Accept into the default folder | `/accept 4b44b161`; omit the ID if only one offer is pending |
| Choose a folder visually | Click **Accept** on an incoming offer |
| Review saved chats | `/history`; use **3:Direct** for private conversation pages |
| Close and remember login | `/quit` |
| Forget the saved login | `/logout` |

Transfer IDs above are examples: use the ID or unique prefix shown in your offer.
The progress bar updates in place; a file is successful only after receiver
verification. Under WSL, Windows folders use paths such as
`/mnt/c/Users/YOUR_NAME/Downloads`.

See the [complete user guide](docs/client-guide.md) for all commands, filters,
keyboard shortcuts, file paths and history behavior.

## Connection problems

- **No discovery reply:** use `--host SERVER_IP`. Check server reachability and
  the TCP/UDP ports. For an existing Tailscale connection, use the server's
  Tailscale IP explicitly; see [LAN and Tailscale](docs/client-guide.md#choose-the-server-address).
- **`TLSV1_ALERT_UNKNOWN_CA`:** the connecting peer rejected the server's
  certificate. Check that client's trusted public certificate against the
  running server's identity; an old clone may carry an older certificate.
- **Pending account:** an admin must approve it before login succeeds.

Follow the [troubleshooting steps](docs/client-guide.md#troubleshooting), including
certificate fingerprint comparison. Do not regenerate certificates just to retry a
connection. [TLS setup](docs/tls-setup.md) explains trust distribution and renewal.

## Development and reference

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m ruff check .
python -m lancollab.server --help
python -m lancollab.client --help
```

The application uses raw TCP/UDP sockets rather than HTTP or WebSocket frameworks.

| Folder | Contents |
| --- | --- |
| `lancollab/client/` | Client interface, history, discovery and file handling |
| `lancollab/server/` | Connections, accounts, chat routing and file relay |
| `lancollab/common/` | Shared protocol, validation and TLS transport |
| `tests/` | Unit and integration tests |
| `scripts/` | Handshake demos and connection/workload checks |
| `docs/` | User guide, protocol and technical reference |

- [Architecture and source map](docs/architecture.md)
- [Protocol specification](docs/protocol-spec.md)
- [Transport overview](docs/diagrams/transport-overview.md)
- [Acceptance checks and evidence](docs/acceptance.md)
- [Assignment checklist and PDF generation](docs/assignment-checklist.md)
- [Technical report](docs/technical-report.md)

Useful tools (run from the project root):

| Command | Purpose |
| --- | --- |
| `python -m scripts.simulate_clients --host SERVER_IP --clients 1` | Diagnose discovery/TCP/TLS against a running server |
| `python -m scripts.local_acceptance` | Isolated 20-client chat/file workload with temporary accounts and keys |
| `python -m scripts.load_test --help` | Configure a workload against another machine |
| `python -m scripts.build_report` | Generate the report PDF; needs `requirements-report.txt` |

Real loopback tests are available; two-host LAN and packet-capture acceptance
still need recorded evidence. For coursework, follow the assignment's academic
integrity rules and ensure every team member can explain the submitted code.
