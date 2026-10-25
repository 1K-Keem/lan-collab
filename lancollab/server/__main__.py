"""Placeholder entry point; TV1 implements the real raw-socket server."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="LAN Collab server (scaffold)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--cert", help="Path to trusted TLS server certificate")
    parser.add_argument("--key", help="Path to private key (never commit)")
    parser.parse_args()
    parser.error("Server is not implemented yet. See GitHub Issues #4 and #10.")


if __name__ == "__main__":
    main()
