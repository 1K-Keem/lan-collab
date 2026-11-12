"""Portable CLI: command grammar, console interaction and receive worker.

Construct a ConsoleSession with an already verified TLS MessageTransport.
CLI never performs TLS itself and never sends credentials over unverified TCP.
"""

from dataclasses import dataclass
import sqlite3
import shutil
import sys

from lancollab.client.history import HistoryStore, local_time
from getpass import getpass
from pathlib import Path
from time import monotonic, sleep
from threading import Event, Thread, Lock
from typing import Callable

from lancollab.client.application import ClientState, CollabClient, safe_display, _USERNAME
from lancollab.client.token_store import TokenStore
from lancollab.client.discovery import DiscoveryClient
from lancollab.client.transfers import TransferError
from lancollab.common.contracts import MessageTransport

_MIN_PASSWORD_LEN = 8  # Must match the server's account policy.

_HELP = (
    "/help, /users, /status, /history [USER|people|sessions|show ID [USER]], /all TEXT, /msg USER TEXT, "
    "/send USER PATH, /accept [ID] [PATH], /reject ID, /cancel ID, "
    "/pending, /approve USER, /reject-user USER, /net, /quit, /logout, "
    "/reply TEXT, /transfers, /files, /search TEXT, /ping, /whoami, /clear, /filter CATEGORY"
)

# Separate lines work in both terminal output and TUI scrollback.
_DETAILED_HELP = (
    "COMMAND HELP",
    "USER = case-sensitive username; TEXT = message; PATH = local file path.",
    "ID = file offer or saved session ID; [USER] = optional username.",
    "",
    "CHAT AND PEOPLE",
    "/all TEXT — Send a global message to everyone else online.",
    "  Example: /all Study group starts at 7 pm",
    "/msg USER TEXT — Send a private message; delivered now if the user is online, stored for their next login if not.",
    "  Example: /msg bob Can you review chapter 3?",
    "/users — List people online.",
    "  Example: /users",
    "/history [USER|people|sessions|show ID [USER]] — Browse saved people and your login sessions.",
    "  TUI: choose a login and press Enter to read it. Tab switches between sessions and people.",
    "  /history people lists people; /history USER opens their complete history across your logins.",
    "  /history show ID [USER] reads a specific login. Readers open at the newest messages.",
    "  Delete or [Delete] removes selected local history after Y confirmation; Esc cancels.",
    "  History must be enabled. Outgoing entries mean server-queued, not read.",
    "",
    "FILES",
    "/send USER PATH — Offer a local file up to 50 MiB; the recipient must accept.",
    '  Example: /send bob "notes/chapter 3.pdf"',
    "/accept [ID] [PATH] — Accept a file; optionally choose its destination folder.",
    '  Example: /accept 123e4567 "~/Documents/Study notes". --dir PATH also works.',
    '  One pending offer: /accept --dir "~/Downloads". Without a folder, use the default.',
    "/reject ID — Decline a pending incoming file offer.",
    "  Example: /reject 123e4567-e89b-42d3-a456-426614174000",
    "/cancel ID — Cancel a pending or active transfer; remove partial downloads.",
    "  Example: /cancel 123e4567-e89b-42d3-a456-426614174000",
    "  Replace example IDs with the actual offer ID. Success requires receiver verification.",
    "",
    "CONNECTION",
    "/status — Show connection details and network measurements.",
    "  Example: /status",
    "/net — Check network quality (latency and packet loss).",
    "  Example: /net",
    "",
    "ADMIN ONLY — regular members cannot use these commands",
    "/pending — List new accounts waiting for approval.",
    "  Example: /pending",
    "/approve USER — Approve a pending account so that user can log in.",
    "  Example: /approve alice",
    "/reject-user USER — Reject a pending account, rather than a file offer.",
    "  Example: /reject-user alice",
    "",
    "HELP AND EXIT",
    "/help — Show this command reference.",
    "  Example: /help",
    "/quit — Close the app; keep your remembered login.",
    "  Example: /quit",
    "/logout — Sign out and forget this device.",
    "  Example: /logout",
    "  Neither exit command deletes local chat history or downloaded files.",
    "",
    "TUI SHORTCUTS (when started with --tui)",
    "  Plain text sends to everyone. Type / for suggestions; Up/Down select; Tab completes.",
    "  Enter completes a partial suggestion or submits a complete command/message.",
    "  Esc dismisses suggestions. Up/Down recall input when suggestions are closed.",
    "  Left/Right, Home/End, Backspace/Delete edit; Ctrl+U clears input; Ctrl+W deletes a word.",
    "  F1 or /help opens this help as a separate screen; Ctrl+C or Esc returns to chat.",
    "  PageUp/PageDown scroll the help screen and the conversation. Ctrl+C/Ctrl+D exit from chat.",
    "  Mouse wheel scrolls; click an online user to message them.",
    "CLI: use your terminal scrollback to review help; Ctrl+C or EOF exits.",
)


_DETAILED_HELP += (
    "DASHBOARD COMMANDS",
    "@USER TEXT — Private message shorthand. Example: @bob Hello",
    "/reply TEXT — Send privately to your last DM peer. Example: /reply Thanks!",
    "  /msg USER TEXT chooses a recipient; /reply TEXT uses the last person you messaged.",
    "  If no DM peer is known yet, use /msg USER TEXT first.",
    "/transfers — List active transfers, short IDs, progress and state.",
    "/accept [ID], /reject [ID], /cancel [ID] — Full UUID or unique prefix; omit if only one eligible transfer exists.",
    "/files — List saved files and sizes in your downloads directory.",
    "/search TEXT — Search local history across sessions by message text or peer (last 50 matches).",
    "/ping — Check connection latency. Use /net for network quality.",
    "/whoami — Show your account, role, server and downloads directory.",
    "/clear — Clear the TUI display without deleting history.",
    "/filter [all|global|dm|files] — Select visible TUI message categories.",
    "F1 help; F2 sidebar; F3 cycle filter; F4 TCP+UDP probe; F5 transfers; F6 accept sole offer.",
    "DISPLAY FILTER GUIDE — the Direct tab also changes who receives plain text.",
    "  1:All — All messages and notices. /filter all",
    "  2:Global — Group chat only. /filter global",
    "  3:Direct — Inbox of everyone you have DMed; click a peer to read the full transcript. /filter dm",
    "  4:Files — File-transfer notices only. /filter files",
    "  Click a tab, use Alt+1/2/3/4, or press F3 to cycle filters.",
    "  Typing 1, 2, 3 or 4 alone is normal message input, not a filter shortcut.",
    "  In a Direct conversation, plain text sends privately to the selected peer; Back or Esc returns to the inbox.",
    "  Outside Direct, plain text still sends to everyone. Filters never delete history or stop incoming messages.",
    "NETWORK CHECKS — manual only: /ping for TCP, /net for UDP, or F4 for both.",
    "  Results appear only when requested; use /status to review connection details.",
    "F8 or /history opens saved sessions; Tab switches to people; Enter opens; Esc goes back. Home/End jump in history.",
    "Ctrl+W deletes a word; Ctrl+K deletes to end; Ctrl+L clears display.",
)


def help_reference(role="member"):
    """Only show account approval commands to people who can use them."""
    admin_section = False
    for line in _DETAILED_HELP:
        if line.startswith("ADMIN ONLY"):
            admin_section = True
        elif line == "HELP AND EXIT":
            admin_section = False
        if role == "admin" or not admin_section:
            yield line


def progress_bar(row, width=14):
    fraction = min(1, row["done"] / row["size"]) if row["size"] else 0
    filled = int(fraction * width)
    return f"[{'█' * filled}{'░' * (width - filled)}] {fraction:.0%} {row['done']}/{row['size']} B · {row['status']}"


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()


def parse_command(line: str) -> Command:
    """Keep Windows path backslashes literal; messages may contain spaces."""
    line = line.strip()
    if not line:
        raise ValueError("Empty command")
    if line.startswith("@"):
        line = "/msg " + line[1:]
    head, _, rest = line.partition(" ")
    action = head.lower()
    if action in {
        "/help",
        "/users",
        "/status",
        "/net",
        "/quit",
        "/logout",
        "/transfers",
        "/files",
        "/ping",
        "/whoami",
        "/clear",
    }:
        if rest.strip():
            raise ValueError(f"{action} does not take arguments")
        return Command(action)
    if action == "/history":
        args = tuple(rest.split())
        if len(args) > 1 and (len(args) not in (2, 3) or args[0] != "show"):
            raise ValueError("Usage: /history [USER|people|sessions|show ID [USER]]")
        return Command(action, args)
    if action == "/filter":
        if rest.strip() not in {"", "all", "global", "dm", "files"}:
            raise ValueError("Usage: /filter [all|global|dm|files]")
        return Command(action, (rest.strip() or "all",))
    if action in {"/reply", "/search"}:
        if not rest.strip():
            raise ValueError(f"Usage: {action} TEXT")
        return Command(action, (rest,))
    if action == "/all":
        if not rest:
            raise ValueError("Usage: /all TEXT")
        return Command(action, (rest,))
    if action in {"/msg", "/send"}:
        to, separator, message = rest.strip().partition(" ")
        if not separator or not message.strip():
            raise ValueError(f"Usage: {action} USER {'TEXT' if action == '/msg' else 'PATH'}")
        if action == "/send":
            message = message.strip()
            if message.startswith('"') and message.endswith('"') and len(message) >= 2:
                message = message[1:-1]
        return Command(action, (to, message))
    if action == "/accept":
        # Only parse the ID/flag; preserve spaces and Windows backslashes in PATH.
        arguments = rest.strip()
        prefix = ""
        if arguments and not arguments.startswith("--"):
            parts = arguments.split(maxsplit=1)
            prefix, arguments = parts[0], parts[1] if len(parts) == 2 else ""
        if not arguments:
            return Command(action, (prefix,))
        parts = arguments.split(maxsplit=1)
        if parts[0] == "--dir":
            if len(parts) != 2:
                raise ValueError("Usage: /accept [TRANSFER_ID] --dir PATH")
            directory = parts[1].strip()
        elif arguments.startswith("--"):
            raise ValueError("Usage: /accept [TRANSFER_ID] [PATH] (or --dir PATH)")
        else:
            directory = arguments
        if directory.startswith(('"', "'")):
            if len(directory) < 2 or directory[-1] != directory[0]:
                raise ValueError("Unclosed quote in destination folder")
            directory = directory[1:-1]
        if not directory.strip():
            raise ValueError("Destination folder cannot be empty")
        return Command(action, (prefix, directory))
    if action in {"/reject", "/cancel"}:
        if len(rest.split()) > 1:
            raise ValueError(f"Usage: {action} [TRANSFER_ID]")
        return Command(action, (rest.strip(),))
    if action == "/pending":
        if rest.strip():
            raise ValueError("/pending does not take arguments")
        return Command(action)
    if action in {"/approve", "/reject-user"}:
        if not rest or len(rest.split()) != 1:
            raise ValueError(f"Usage: {action} USERNAME")
        return Command(action, (rest.strip(),))

    raise ValueError(f"Unknown command: {safe_display(head)}. Type /help")


class AuthenticationError(RuntimeError):
    """A verified server explicitly rejected the supplied credentials/token."""


class RegistrationError(RuntimeError):
    """A verified server rejected the sign-up request (e.g. username taken)."""


class ConsoleSession:
    """Authenticated session UI; input loop never calls transport.receive()."""

    def __init__(
        self,
        transport: MessageTransport,
        *,
        output: Callable[[str], None] = print,
        input_line: Callable[[str], str] = input,
        downloads_dir: str | Path = "downloads",
        udp: DiscoveryClient | None = None,
        server_host: str | None = None,
        token_store: TokenStore | None = None,
        history: HistoryStore | None = None,
        history_server: str = "",
    ) -> None:
        self._udp_lock = Lock()
        self.display_filter = "all"
        self.clear_generation = 0
        self.output = output
        self._drain_lock = Lock()
        self._progress_line = ""
        self._command_prompt = False
        self.input_line = input_line
        self.udp = udp
        self.server_host = server_host
        self.token_store = token_store
        self.client = CollabClient(
            transport,
            auth_allowed=True,
            downloads_dir=downloads_dir,
            history=history,
            history_server=history_server,
        )
        # Security invariant: production callers must pass a verified-TLS transport.
        self.stop = Event()
        self.reader: Thread | None = None
        self.notifier: Thread | None = None

    def history_people(self):
        if self.client.history is None or self.client.username is None:
            raise ValueError("Local history is unavailable")
        return self.client.history.people(self.client.history_server, self.client.username)

    def history_sessions(self, peer=None):
        if self.client.history is None or self.client.username is None:
            raise ValueError("Local history is unavailable")
        return self.client.history.session_records(
            self.client.history_server, self.client.username, peer
        )

    def history_archive(self, peer=None):
        if self.client.history is None or self.client.username is None:
            raise ValueError("Local history is unavailable")
        return self.client.history.archive(self.client.history_server, self.client.username, peer)

    def delete_history(self, *, session_id=None, peer=None):
        client = self.client
        with client._lock:
            if client.history is None or client.username is None:
                raise ValueError("Local history is unavailable")
            count = client.history.delete(
                client.history_server,
                client.username,
                session=session_id,
                peer=peer,
                keep_session=session_id == client.session_id,
            )
            with client._direct_lock:
                if session_id is None or session_id == client.session_id:
                    client._direct_messages = [
                        row
                        for row in client._direct_messages
                        if peer is not None and row[3] != peer
                    ]
                client.direct_revision += 1
            return count

    def history_label(self, record):
        current = " · current" if record.id == self.client.session_id else ""
        return (
            f"{local_time(record.started)} · {record.count} msgs · {record.id or 'legacy'}{current}"
        )

    def history_transcript(self, session_id, peer=None):
        session_id = "" if session_id == "legacy" else session_id
        record = next((r for r in self.history_sessions(peer) if r.id == session_id), None)
        if record is None:
            raise ValueError("Saved session not found for this account and server")
        rows = self.client.history.transcript(
            self.client.history_server, self.client.username, session_id, peer
        )
        return record, rows

    @staticmethod
    def history_message(row):
        timestamp, direction, kind, peer, text = row
        channel = "DM" if kind == "CHAT_PRIVATE" else "All"
        sender = (f"you → {peer}" if channel == "DM" else "you") if direction == "sent" else peer
        return safe_display(f"{local_time(timestamp)} [{channel}] {sender}: {text}")

    def direct_conversations(self):
        """Private conversations, newest last message first: (peer, timestamp, text).

        Uses saved history when enabled; otherwise the current session's
        in-memory DMs, so the Direct inbox works with --no-history.
        """
        username = self.client.username
        if username is None:
            return []
        if self.client.history is not None:
            return [
                row
                for row in self.client.history.direct_conversations(
                    self.client.history_server, username
                )
                if row[0] != username
            ]
        latest = {}
        for timestamp, _direction, kind, peer, text in self.client.direct_messages():
            if kind == "CHAT_PRIVATE" and peer != username:
                latest.pop(peer, None)
                latest[peer] = (timestamp, text)
        return [(peer, *last) for peer, last in reversed(list(latest.items()))]

    def direct_transcript(self, peer):
        """Full private transcript with ``peer`` across sessions, oldest first.

        Rows are (timestamp, direction, kind, peer, text), same as
        HistoryStore.transcript. No 50-message cap is applied.
        """
        username = self.client.username
        if username is None or peer == username:
            return []
        if self.client.history is not None:
            return list(
                self.client.history.direct_transcript(self.client.history_server, username, peer)
            )
        return [
            row
            for row in self.client.direct_messages()
            if row[2] == "CHAT_PRIVATE" and row[3] == peer
        ]

    def _drain(self) -> None:
        with self._drain_lock:
            notices = self.client.drain_notices()
            if notices:
                self._clear_progress()
            for notice in notices:
                if notice.kind == "chat":
                    tag, separator, body = notice.text.partition(" ")
                    tag = {"[CHAT_PRIVATE]": "[DM]", "[CHAT_GLOBAL]": "[All]"}.get(tag, tag)
                    text = tag + separator + body
                    self.output(text)
                else:
                    self.output(f"[{notice.kind}] {notice.text}")
            self._render_progress()
            if notices and self._command_prompt and not self._progress_line:
                if self.output is print and sys.stdout.isatty():
                    sys.stdout.write(self._input_prompt())
                    sys.stdout.flush()

    def _input_prompt(self) -> str:
        if not self._command_prompt:
            return ""
        try:
            import readline

            typed = readline.get_line_buffer()
        except ImportError:
            typed = ""
        return "lan-collab> " + typed

    def _clear_progress(self) -> None:
        if self._progress_line:
            sys.stdout.write("\r" + " " * len(self._progress_line) + "\r")
            sys.stdout.flush()
            self._progress_line = ""

    def _render_progress(self) -> None:
        # The curses UI renders its own dock. Redirected output and custom output
        # consumers get lifecycle notices only, without terminal control bytes.
        if self.output is not print or not sys.stdout.isatty():
            return
        rows = [r for r in self.client.files.snapshot() if r["status"] not in {"offer", "waiting"}]
        pieces = []
        for row in rows:
            filled = min(16, row["done"] * 16 // row["size"]) if row["size"] else 16
            pieces.append(
                f"{row['id'][:8]} [{'#' * filled}{'-' * (16 - filled)}] "
                f"{row['done']}/{row['size']} B ({row['status']}; unverified)"
            )
        prompt = self._input_prompt()
        width = max(1, shutil.get_terminal_size().columns - 1)
        progress = " | ".join(pieces)
        if progress and prompt:
            progress = progress[: max(0, width - len(prompt) - 3)]
            line = (progress + " | " if progress else "") + prompt
        else:
            line = progress
        line = line[:width]
        if line == self._progress_line:
            return
        had_progress = bool(self._progress_line)
        self._clear_progress()
        if line:
            sys.stdout.write("\r" + line)
        elif had_progress and prompt:
            sys.stdout.write(prompt)
        sys.stdout.flush()
        self._progress_line = line

    def start_reader(self) -> None:
        if self.reader is not None:
            raise RuntimeError("Already started")
        self.reader = Thread(
            target=self.client.receive_loop,
            args=(self.stop,),
            daemon=True,
            name="lancollab-receiver",
        )
        self.reader.start()
        self.notifier = Thread(target=self._notices_loop, daemon=True, name="lancollab-console")
        self.notifier.start()

    def _notices_loop(self) -> None:
        while not self.stop.wait(0.10):
            self._drain()

    def _wait_for(self, state: ClientState, *, timeout: float = 10.0) -> None:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if self.client.auth_error is not None:
                raise AuthenticationError(self.client.auth_error)
            if state is ClientState.WAIT_AUTH and self.client.registration_result is not None:
                if not self.client.registration_result:
                    raise RegistrationError("Registration rejected; see the server response")
                return
            if self.client.state is state:
                self._drain()
                return
            if self.client.state is ClientState.CLOSED:
                self._drain()
                raise ConnectionError("Session closed during handshake")
            sleep(0.025)
        self.client.close()
        raise TimeoutError(f"Timed out awaiting {state.value}")

    def authenticate(self, username: str, password: str, *, remember_me: bool = False) -> None:
        self.start_reader()
        deadline = monotonic() + 10
        self.client.login(username, password, remember_me=remember_me)
        self._wait_for(ClientState.AUTHENTICATED, timeout=max(0.01, deadline - monotonic()))
        if (
            remember_me
            and self.client.issued_token is not None
            and self.token_store is not None
            and self.server_host is not None
        ):
            self.token_store.save(self.server_host, username, self.client.issued_token)
            self.output(f"[auth] Remembered login saved to {self.token_store.describe()}")

    def authenticate_with_token(self, token: str) -> None:
        """Log in with a stored remember-me token; raises if it is dead."""
        self.start_reader()
        deadline = monotonic() + 10
        self.client.login_with_token(token)
        self._wait_for(ClientState.AUTHENTICATED, timeout=max(0.01, deadline - monotonic()))

    def register(self, username: str, password: str) -> None:
        self.start_reader()
        self.client.register(username, password)
        self._wait_for(ClientState.WAIT_AUTH, timeout=10.0)
        self._drain()

    def refresh_pending_quietly(self) -> None:
        """Background sidebar refresh: update pending_users without printing.

        The TUI timer calls this instead of /pending so the admin's chat is
        not flooded with one "Pending users" line per refresh.
        """
        try:
            self.client.list_pending(quiet=True)
        except (RuntimeError, OSError):
            pass  # Not an admin, not authenticated, or the link is down.

    def execute(self, command: Command) -> bool:
        """Return False for /quit, True otherwise; errors become visible notices."""
        try:
            if command.name == "/help":
                for line in help_reference(self.client.role):
                    self.output(line)
            elif command.name == "/reply":
                target = self.client.reply_target
                if target is None:
                    raise ValueError("No DM reply target yet; use /msg USER TEXT")
                self.client.send_private(target, command.args[0])
            elif command.name == "/ping":
                self.client.ping()
                self.output("TCP/TLS probe sent; awaiting PONG…")
            elif command.name == "/whoami":
                self.output(
                    safe_display(
                        f"{self.client.username} ({self.client.role}) | server {self.server_host or 'unknown'} | downloads {self.client.files.downloads_dir.resolve()}"
                    )
                )
            elif command.name == "/clear":
                self.clear_generation += 1
                self.output("[display] Conversation display cleared; saved history is unchanged.")
            elif command.name == "/filter":
                self.display_filter = command.args[0]
                self.output(f"[display] TUI filter: {self.display_filter}")
            elif command.name == "/transfers":
                rows = self.client.files.snapshot()
                for row in rows:
                    self.output(
                        safe_display(
                            f"{row['id']} {row['direction']} {row['peer']}: {row['name']} {progress_bar(row)}"
                        )
                    )
                if not rows:
                    self.output("No active transfers.")
            elif command.name == "/files":
                directory = self.client.files.downloads_dir
                paths = sorted(directory.iterdir()) if directory.exists() else []
                files = [
                    path
                    for path in paths
                    if path.is_file()
                    and not path.is_symlink()
                    and not path.name.startswith(".lancollab-")
                ]
                for path in files:
                    self.output(safe_display(f"{path.name} — {path.stat().st_size} bytes"))
                if not files:
                    self.output("No saved files.")
            elif command.name == "/search":
                if self.client.history is None or self.client.username is None:
                    raise ValueError("Local history is unavailable")
                rows = self.client.history.search(
                    self.client.history_server, self.client.username, command.args[0]
                )
                for row in rows:
                    self.output(self.history_message(row))
                if not rows:
                    self.output("No matching messages.")
            elif command.name == "/history":
                if self.client.history is None or self.client.username is None:
                    self.output("Local history is unavailable.")
                else:
                    store = self.client.history
                    server = self.client.history_server
                    account = self.client.username
                    if command.args == ("people",):
                        people = self.history_people()
                        self.output("Saved people — your account on this server · read only")
                        for peer, timestamp, _text, count in people:
                            self.output(
                                safe_display(
                                    f"  {peer} | {count} messages | {local_time(timestamp)}"
                                )
                            )
                        if not people:
                            self.output("No saved people yet.")
                        self.output(
                            "Use /history sessions, then /history show ID USER to read a conversation."
                        )
                    elif not command.args or command.args == ("sessions",):
                        records = self.history_sessions()
                        self.output("Saved login sessions — read only (newest first)")
                        for record in records:
                            self.output(safe_display(self.history_label(record)))
                        self.output(
                            "Use /history show ID to view a session; legacy uses ID legacy."
                        )
                        if not records:
                            self.output("No saved sessions.")
                    elif len(command.args) in (2, 3) and command.args[0] == "show":
                        peer = command.args[2] if len(command.args) == 3 else None
                        record, rows = self.history_transcript(command.args[1], peer)
                        self.output(safe_display(self.history_label(record)))
                        for row in rows:
                            self.output(self.history_message(row))
                        if not rows:
                            self.output("No saved messages in this session.")
                    elif command.args[0] == "all":
                        rows = store.archive(server, account)
                        records = {record.id: record for record in self.history_sessions()}
                        current = None
                        for session, timestamp, direction, kind, peer, text in rows:
                            if session != current:
                                current = session
                                label = (
                                    "before sessions were tracked"
                                    if session == ""
                                    else f"started {local_time(records[session].started)}"
                                )
                                self.output(f"── Session {label} ──")
                            self.output(
                                self.history_message((timestamp, direction, kind, peer, text))
                            )
                        if not rows:
                            self.output("No saved messages.")
                    else:
                        peer = command.args[0] if command.args else None
                        rows = self.history_archive(peer)
                        for row in rows:
                            self.output(self.history_message(row[1:]))
                        if not rows:
                            self.output("No saved messages for this person.")
            elif command.name == "/users":
                users = sorted(self.client.online_users.items())
                if not users:
                    self.output("Online users: none (or snapshot not yet received)")
                for name, _address in users:
                    self.output(safe_display(name))
            elif command.name == "/status":
                self.output(f"TCP/TLS: {self.client.state.value}")
                if self.udp is not None:
                    q = self.udp.quality
                    self.output(
                        f"UDP: {q.received}/{q.sent}, loss {q.loss_percent:.1f}%, RTT {q.last_rtt_ms}"
                    )
            elif command.name == "/net":
                if self.udp is None or not self.server_host:
                    self.output("UDP unavailable; use --host or --discover")
                else:
                    with self._udp_lock:
                        q = self.udp.heartbeat(self.server_host)
                    self.output(
                        f"UDP heartbeat: {q.received}/{q.sent}, loss {q.loss_percent:.1f}%, RTT {q.last_rtt_ms}"
                    )
            elif command.name == "/all":
                self.client.send_global(command.args[0])
            elif command.name == "/msg":
                self.client.send_private(*command.args)
            elif command.name == "/send":
                transfer_id = self.client.offer_file(command.args[1], command.args[0])
                self.output(f"File offered: {transfer_id} (waiting for consent)")
            elif command.name == "/accept":
                self.client.files.accept(
                    self.client.files.resolve_id(command.args[0] if command.args else "", "accept"),
                    directory=command.args[1] if len(command.args) > 1 else None,
                )
            elif command.name == "/reject":
                self.client.files.reject(
                    self.client.files.resolve_id(command.args[0] if command.args else "", "reject")
                )
            elif command.name == "/cancel":
                self.client.files.cancel(
                    self.client.files.resolve_id(command.args[0] if command.args else "", "cancel")
                )
            elif command.name == "/quit":
                # Close the app, stay logged in: just close the transport.
                # The server sees a disconnect (session cleaned up) and the
                # token is NOT revoked, so the next launch auto-logs in.
                self.client.close()
                return False
            elif command.name == "/logout":
                # Forget this device even if the peer disconnected before LOGOUT.
                try:
                    if self.client.state in {ClientState.WAIT_AUTH, ClientState.AUTHENTICATED}:
                        self.client.logout()
                    else:
                        self.output("[auth] Server token revocation could not be confirmed.")
                except (RuntimeError, ValueError, OSError) as error:
                    self.output(
                        "[auth] Server token revocation could not be confirmed: "
                        + safe_display(str(error))
                    )
                finally:
                    self.client.close()
                    if self.token_store is not None and self.server_host:
                        self.token_store.delete(self.server_host)
                return False
            elif command.name == "/pending":
                self.client.list_pending()
            elif command.name == "/approve":
                self.client.review(command.args[0], "approve")
            elif command.name == "/reject-user":
                self.client.review(command.args[0], "reject")
            else:
                raise ValueError("Unknown command")
        except (TransferError, RuntimeError, ValueError, OSError, sqlite3.Error) as error:
            self.output(f"[error] {safe_display(str(error))}")
        self._drain()
        return True

    def run_commands(self) -> None:
        self.output(_HELP)
        try:
            while self.client.state is not ClientState.CLOSED:
                try:
                    self._command_prompt = True
                    try:
                        line = self.input_line("lan-collab> ")
                    finally:
                        self._command_prompt = False
                        with self._drain_lock:
                            self._clear_progress()
                    command = parse_command(line)
                except (EOFError, KeyboardInterrupt):
                    # Closing the app (Ctrl+C, terminal closed, stdin EOF)
                    # is not logging out: the token is kept, like /quit.
                    return
                except ValueError as error:
                    self.output(f"[error] {error}")
                    continue
                if not self.execute(command):
                    return
        finally:
            self.close()

    def close(self) -> None:
        self.stop.set()
        self.client.close()
        if self.reader is not None and self.reader.is_alive():
            self.reader.join(timeout=1)
        if self.notifier is not None and self.notifier.is_alive():
            self.notifier.join(timeout=1)
        self._drain()


def prompt_welcome() -> tuple[str, str, str]:
    """Ask Login or Sign up BEFORE connecting, to meet the server AUTH deadline.

    Invalid input is retried here, so no TLS connection is ever opened for it.
    Returns (choice, username, password); choice is 'login' or 'signup'.
    """
    while True:
        choice = input("Login or Sign up? [login/signup]: ").strip().lower()
        if choice in ("login", "signup"):
            break
        print("Type 'login' or 'signup'.")
    while True:
        username = input("Username: ").strip()
        if _USERNAME.fullmatch(username) is not None:
            break
        print("Username must be 1-32 characters: letters, digits, '_', '.', '-'.")
    if choice == "login":
        while True:
            password = getpass("Password: ")
            if password:
                return choice, username, password
            print("Password cannot be empty.")
    while True:
        password = getpass("Password: ")
        if len(password) < _MIN_PASSWORD_LEN:
            print(f"Password must be at least {_MIN_PASSWORD_LEN} characters.")
            continue
        confirm = getpass("Confirm password: ")
        if password == confirm:
            return choice, username, password
        print("Passwords do not match; re-enter both.")
