"""Concurrent TCP/TLS and UDP discovery server with SQLite account auth."""

import argparse
import getpass
import logging
from pathlib import Path

from lancollab.common.tls import create_server_context
from lancollab.common.devcerts import ensure_server_certs
from lancollab.server.accounts import AccountError, AccountService
from lancollab.server.application import CollabServer
from lancollab.server.queue import MessageQueue
from lancollab.server.tokens import TokenService
from lancollab.server.connections import ConnectionServer


def _add_admin(db_path: Path, username: str) -> int:
    password = getpass.getpass("Admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return 1
    accounts = AccountService(db_path)
    try:
        accounts.create_account(username, password, role="admin", status="active")
    except AccountError as error:
        print(f"Cannot create admin: {error.code}")
        return 1
    finally:
        accounts.close()
    print(f"Admin '{username}' created.")
    return 0


def _change_password(db_path: Path, username: str) -> int:
    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm new password: ")
    if password != confirm:
        print("Passwords do not match.")
        return 1
    accounts = AccountService(db_path)
    try:
        accounts.change_password(username, password)
    except AccountError as error:
        print(f"Cannot change password: {error.code}")
        return 1
    finally:
        accounts.close()
    print(f"Password changed for '{username}'.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--udp-port", type=int, default=40001)
    parser.add_argument("--max-clients", type=int, default=32)
    parser.add_argument("--cert", default="certs/direct-trust/server-public.pem")
    parser.add_argument("--key", default="certs/direct-trust/server-secret.pem")
    parser.add_argument("--db", type=Path, default=Path("accounts.db"))
    parser.add_argument("--add-admin", metavar="USERNAME", help="Create first admin and exit")
    parser.add_argument(
        "--change-password", metavar="USERNAME", help="Change an account password and exit"
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Activate new accounts on signup/first login; first account in an empty DB becomes admin",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(threadName)s: %(message)s")
    if args.add_admin:
        return _add_admin(args.db, args.add_admin)
    if args.change_password:
        return _change_password(args.db, args.change_password)
    accounts = tokens = queue = server = None
    try:
        if (args.cert, args.key) == (
            "certs/direct-trust/server-public.pem",
            "certs/direct-trust/server-secret.pem",
        ):
            ensure_server_certs()
        context = create_server_context(args.cert, args.key)
        accounts = AccountService(args.db)
        tokens = TokenService(args.db)
        queue = MessageQueue(args.db)
        server = ConnectionServer(
            context,
            port=args.port,
            udp_port=args.udp_port,
            max_clients=args.max_clients,
            accounts=accounts,
            handler=CollabServer(accounts, tokens, queue, auto_approve=args.auto_approve),
        )
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Server failed: {error}\n")
    finally:
        if server is not None:
            server.close()
        if queue is not None:
            queue.close()
        if tokens is not None:
            tokens.close()
        if accounts is not None:
            accounts.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
