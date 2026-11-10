"""TV3 portable CLI: command grammar, console interaction and receive worker.

Construct a ConsoleSession with an already verified MessageTransport from TV2.
CLI never performs TLS itself and never sends credentials over unverified TCP.
"""

from dataclasses import dataclass
import sqlite3

from lancollab.client.history import HistoryStore
from getpass import getpass
from pathlib import Path
from time import monotonic, sleep
from threading import Event, Thread
from typing import Callable

from lancollab.client.application import ClientState, CollabClient, safe_display
from lancollab.client.token_store import TokenStore
from lancollab.client.discovery import DiscoveryClient
from lancollab.client.transfers import TransferError
from lancollab.common.contracts import MessageTransport

_HELP = (
    "/help, /users, /status, /history [USER], /all TEXT, /msg USER TEXT, "
    "/send USER PATH, /accept ID, /reject ID, /cancel ID, "
    "/pending, /approve USER, /reject-user USER, /net, /quit, /logout"
)


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()


def parse_command(line: str) -> Command:
    """Keep Windows path backslashes literal; messages may contain spaces."""
    line = line.strip()
    if not line:
        raise ValueError("Empty command")
    head, _, rest = line.partition(" ")
    action = head.lower()
    if action in {"/help", "/users", "/status", "/net", "/quit", "/logout"}:
        if rest.strip():
            raise ValueError(f"{action} does not take arguments")
        return Command(action)
    if action == "/history":
        args = tuple(rest.split())
        if len(args) > 1:
            raise ValueError("Usage: /history [USER]")
        return Command(action, args)
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
    if action in {"/accept", "/reject", "/cancel"}:
        if not rest or len(rest.split()) != 1:
            raise ValueError(f"Usage: {action} TRANSFER_ID")
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
        self.output = output
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

    def _drain(self) -> None:
        for notice in self.client.drain_notices():
            self.output(f"[{notice.kind}] {notice.text}")

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
                    raise RuntimeError("Registration rejected; see the server response")
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

    def execute(self, command: Command) -> bool:
        """Return False for /quit, True otherwise; errors become visible notices."""
        try:
            if command.name == "/help":
                self.output(_HELP)
                self.output(
                    "/quit closes and stays logged in; /logout logs out and forgets this device."
                )
            elif command.name == "/history":
                if self.client.history is None or self.client.username is None:
                    self.output("Local history is unavailable.")
                else:
                    rows = self.client.history.recent(
                        self.client.history_server,
                        self.client.username,
                        command.args[0] if command.args else None,
                    )
                    for timestamp, direction, kind, peer, text in rows:
                        self.output(
                            safe_display(f"{timestamp} [{kind}] {direction} {peer}: {text}")
                        )
                    if not rows:
                        self.output("No saved messages.")
            elif command.name == "/users":
                users = sorted(self.client.online_users.items())
                if not users:
                    self.output("Online users: none (or snapshot not yet received)")
                for name, (ip, port) in users:
                    self.output(f"{safe_display(name)}  {safe_display(ip)}:{port}")
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
                self.client.files.accept(command.args[0])
            elif command.name == "/reject":
                self.client.files.reject(command.args[0])
            elif command.name == "/cancel":
                self.client.files.cancel(command.args[0])
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
                    command = parse_command(self.input_line("lan-collab> "))
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
    Returns (choice, username, password); choice is 'login' or 'signup'."""
    while True:
        choice = input("Login or Sign up? [login/signup]: ").strip().lower()
        if choice in ("login", "signup"):
            break
        print("Type 'login' or 'signup'.")
    username = input("Username: ").strip()
    password = getpass("Password: ")
    if choice == "signup":
        confirm = getpass("Confirm password: ")
        if password != confirm:
            raise ValueError("Passwords do not match")
    return choice, username, password
