"""Client CLI entry point: resolve the server, verify TLS, then authenticate."""

import argparse
from hashlib import sha256
from pathlib import Path
import sys
import sqlite3

from lancollab.client.cli import AuthenticationError, ConsoleSession, prompt_welcome
from lancollab.client.history import HistoryStore
from lancollab.client.discovery import DiscoveryClient, select_server
from lancollab.client.token_store import TokenStore, _config_dir
from lancollab.common.tls import resolve_trusted_cert
from lancollab.common.socket_transport import SocketTransport


def _ask_remember_me() -> bool:
    """Ask once, before login (the flag travels inside AUTH_REQUEST)."""
    try:
        return input("Remember me on this machine? [y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN Collab portable client (TV3)")
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
                    print("Saved login expired; please log in again.", file=sys.stderr)
            # 2) No (usable) token: ask for credentials BEFORE connecting, to
            # honor the server's auth deadline.
            choice, username, password = prompt_welcome()
            remember_me = choice != "signup" and _ask_remember_me()
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
                if choice == "signup":
                    shell.register(username, password)
                    print(
                        "Registered. Waiting for admin approval — run the client again after approval."
                    )
                    return 0
                shell.authenticate(username, password, remember_me=remember_me)
                _run_interface(shell, args.tui)
            finally:
                shell.close()

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
