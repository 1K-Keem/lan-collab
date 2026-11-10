"""TV3 portable CLI: command grammar, console interaction and receive worker.

Construct a ConsoleSession with an already verified MessageTransport from TV2.
CLI never performs TLS itself and never sends credentials over unverified TCP.
"""

from dataclasses import dataclass
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
    "/help, /users, /status, /all TEXT, /msg USER TEXT, "
    "/send USER PATH, /accept ID, /reject ID, /cancel ID, "
    "/pending, /approve USER, /reject-user USER, /net, /quit"
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
    if action in {"/help", "/users", "/status", "/net", "/quit"}:
        if rest.strip():
            raise ValueError(f"{action} does not take arguments")
        return Command(action)
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
    ) -> None:
        self.output = output
        self.input_line = input_line
        self.udp = udp
        self.server_host = server_host
        self.token_store = token_store
        self.client = CollabClient(transport, auth_allowed=True, downloads_dir=downloads_dir)
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
                if self.client.state in {ClientState.WAIT_AUTH, ClientState.AUTHENTICATED}:
                    username = self.client.username
                    self.client.logout()  # server revokes this session's token
                    if self.token_store is not None and username and self.server_host:
                        self.token_store.delete(self.server_host)  # logout forgets the token
                return False
            elif command.name == "/pending":
                self.client.list_pending()
            elif command.name == "/approve":
                self.client.review(command.args[0], "approve")
            elif command.name == "/reject-user":
                self.client.review(command.args[0], "reject")
            else:
                raise ValueError("Unknown command")
        except (TransferError, RuntimeError, ValueError, OSError) as error:
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
                    command = Command("/quit")
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
