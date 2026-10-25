"""Placeholder entry point; TV3 implements the interactive TCP/TLS client."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="LAN Collab client (scaffold)")
    parser.add_argument("--host", help="Server IP/hostname (manual discovery fallback)")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--server-name", default="lan-collab.local")
    parser.parse_args()
    parser.error("Client is not implemented yet. See GitHub Issues #7 and #9.")


if __name__ == "__main__":
    main()
