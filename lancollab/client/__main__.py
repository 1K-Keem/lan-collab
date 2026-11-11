"""Client CLI entry point: resolve the server, verify TLS, then authenticate."""

import argparse
from hashlib import sha256
from pathlib import Path
import sys
import sqlite3
import ssl

from lancollab.client.cli import (
    AuthenticationError,
    ConsoleSession,
    RegistrationError,
    prompt_welcome,
)
from lancollab.client.history import HistoryStore
from lancollab.client.branding import print_welcome
from lancollab.client.discovery import DiscoveryClient, select_server
from lancollab.client.token_store import TokenStore, _config_dir
from lancollab.common.tls import resolve_trusted_cert, tls_failure_detail
from lancollab.common.socket_transport import SocketTransport


_AUTH_FAILURES = {
    "AUTH_FAILED": "wrong username or password",
    "ACCOUNT_PENDING": "account pending admin approval; ask an admin, then log in again",
    "ACCOUNT_REJECTED": "account rejected by an admin; contact an admin",
    "TOKEN_EXPIRED": "saved login expired; log in with your password",
    "TOKEN_REVOKED": "saved login was revoked; log in with your password",
}


def _auth_failure(error_code: str, username: str) -> str:
    reason = _AUTH_FAILURES.get(error_code, error_code)
    return f"Login failed for '{username}': {reason}."


def _ask_remember_me() -> bool:
    """Ask once, before login (the flag travels inside AUTH_REQUEST)."""
    return input("Remember me on this machine? [y/N]: ").strip().lower() == "y"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN Collab terminal client")
    parser.add_argument("--host", help="Explicit server IP/hostname (skips discovery)")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument(
        "--server-name", default="lan-collab.local", help="TLS verified DNS identity"
    )
    parser.add_argument(
        "--trusted-cert",
        help="Trusted server certificate (default: LANCOLLAB_TRUSTED_CERT or bundled server-public.pem)",
    )
    parser.add_argument("--udp-port", type=int, default=40001)
    parser.add_argument(
        "--discover", action="store_true", help="Broadcast for server if --host omitted"
    )
    parser.add_argument("--downloads-dir", type=Path, default=Path("downloads"))
    parser.add_argument("--no-history", action="store_true", help="Do not store local chat history")
    parser.add_argument("--history-file", type=Path, help="Local SQLite chat archive")
    parser.add_argument(
        "--tui", action="store_true", help="Full-screen terminal UI (requires curses)"
    )
    args = parser.parse_args(argv)
    if args.tui:
        try:
            import curses  # noqa: F401
        except ImportError:
            parser.error("This Python has no curses support; use the CLI without --tui")
    history = None
    if not args.host and not args.discover:
        parser.error("Use --host or --discover (manual --host is always a fallback)")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid TCP port")
    try:
        trusted_cert = resolve_trusted_cert(args.trusted_cert)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        print_welcome()
        store = TokenStore()
        with DiscoveryClient(udp_port=args.udp_port) as discovery:
            found = discovery.discover() if args.discover and not args.host else None
            endpoint = select_server(found, host=args.host, port=args.port)
            if not args.no_history:
                history = HistoryStore(args.history_file or _config_dir() / "history.sqlite3")
            identity = sha256(Path(trusted_cert).read_bytes()).hexdigest()
            history_server = f"{args.server_name}:{endpoint.tcp_port}:{identity}"
            # Manual IP does not bypass certificate verification of --server-name.
            # 1) Try a stored remember-me token first: new TLS, no password typed.
            auth_message = ""
            auth_username = ""
            auth_choice = "login"
            saved = store.load(endpoint.host)
            if saved is not None:
                _saved_user, saved_token = saved
                try:
                    transport = SocketTransport.connect_verified(
                        host=endpoint.host,
                        port=endpoint.tcp_port,
                        server_hostname=args.server_name,
                        trusted_cert=trusted_cert,
                    )
                    shell = ConsoleSession(
                        transport,
                        downloads_dir=args.downloads_dir,
                        udp=discovery,
                        server_host=endpoint.host,
                        token_store=store,
                        history=history,
                        history_server=history_server,
                    )
                    try:
                        shell.authenticate_with_token(saved_token)
                        _run_interface(shell, args.tui)
                        return 0
                    finally:
                        shell.close()
                except AuthenticationError:
                    # Dead token (expired / revoked / account disabled): forget
                    # it and fall through to the Login / Sign up prompt.
                    store.delete(endpoint.host)
                    auth_message = "Saved login expired; please log in again."
                    if not args.tui:
                        print(auth_message, file=sys.stderr)

            # 2) No (usable) token: ask for credentials BEFORE connecting, to
            # honor the server's auth deadline. A failed attempt returns to
            # this menu; every attempt opens a fresh TLS connection.
            def connect() -> ConsoleSession:
                return ConsoleSession(
                    SocketTransport.connect_verified(
                        host=endpoint.host,
                        port=endpoint.tcp_port,
                        server_hostname=args.server_name,
                        trusted_cert=trusted_cert,
                    ),
                    downloads_dir=args.downloads_dir,
                    udp=discovery,
                    server_host=endpoint.host,
                    token_store=store,
                    history=history,
                    history_server=history_server,
                )

            while True:
                if args.tui:
                    from lancollab.client.auth_tui import prompt_auth

                    credentials = prompt_auth(
                        f"{endpoint.host}:{endpoint.tcp_port}",
                        auth_message,
                        auth_username,
                        auth_choice,
                    )
                    if credentials is None:
                        return 0
                    choice, username, password, remember_me = credentials
                    auth_username, auth_choice = username, choice
                else:
                    choice, username, password = prompt_welcome()
                    remember_me = choice == "login" and _ask_remember_me()
                if choice == "signup":
                    shell = connect()
                    try:
                        shell.register(username, password)
                    except RegistrationError:
                        auth_message = "Sign-up rejected; username may be taken. Try another name."
                        if not args.tui:
                            print(auth_message, file=sys.stderr)
                        continue
                    finally:
                        shell.close()
                    # The server closes the connection after REGISTER_RESULT, so
                    # log in on a new one; active accounts get straight in and
                    # pending ones return to this menu below.
                    if not args.tui:
                        print("Registration finished; logging in.")
                shell = connect()
                try:
                    try:
                        shell.authenticate(username, password, remember_me=remember_me)
                    except AuthenticationError as error:
                        auth_message = _auth_failure(str(error), username)
                        auth_choice = "login"
                        if not args.tui:
                            print(auth_message, file=sys.stderr)
                        continue
                    _run_interface(shell, args.tui)
                    return 0
                finally:
                    shell.close()

    except EOFError:
        print("\nNo input available; exiting.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except ssl.SSLError as error:
        print(
            f"TLS connection failed: {tls_failure_detail(error)}\n"
            f"Trusted certificate: {trusted_cert}\nExpected server name: {args.server_name}",
            file=sys.stderr,
        )
        return 1
    except (ConnectionError, OSError, RuntimeError, TimeoutError, sqlite3.Error) as error:
        print(f"Client stopped: {error}", file=sys.stderr)
        return 1
    finally:
        if history is not None:
            history.close()
    return 0


def _run_interface(shell, tui):
    if tui:
        from lancollab.client.tui import run_tui

        run_tui(shell)
    else:
        shell.run_commands()


if __name__ == "__main__":
    sys.exit(main())
