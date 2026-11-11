"""Run an isolated production-server workload on loopback; never touch real accounts/keys."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import platform
import secrets
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, process_time

from lancollab.common.devcerts import generate_dev_certs
from lancollab.common.tls import create_server_context
from lancollab.server.accounts import AccountService
from lancollab.server.application import CollabServer
from lancollab.server.connections import ConnectionServer
from scripts.load_test import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/local-benchmark.json"))
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--messages", type=int, default=5)
    args = parser.parse_args()
    if not 2 <= args.clients <= 32 or args.messages < 1:
        parser.error("Use 2–32 clients and at least one message per client")
    with TemporaryDirectory(prefix="lan-collab-acceptance-") as directory:
        root = Path(directory)
        certs = generate_dev_certs(root / "certs")
        accounts = AccountService(root / "accounts.db")
        server = thread = None
        try:
            credentials = [
                {"username": f"user{i}", "password": secrets.token_urlsafe(24)}
                for i in range(args.clients)
            ]
            for entry in credentials:
                accounts.create_account(**entry, status="active")
            (root / "accounts.json").write_text(json.dumps(credentials))
            (root / "payload.bin").write_bytes(bytes(range(256)) * (40 * 1024))
            server = ConnectionServer(
                create_server_context(str(certs.server_cert), str(certs.server_key)),
                port=0,
                udp_port=0,
                handler=CollabServer(accounts),
            )
            thread = Thread(target=server.serve_forever)
            thread.start()
            started, cpu_started = monotonic(), process_time()
            result = asdict(
                run(
                    argparse.Namespace(
                        accounts=root / "accounts.json",
                        host="127.0.0.1",
                        port=server.port,
                        server_name="lan-collab.local",
                        trusted_cert=str(certs.server_cert),
                        clients=args.clients,
                        messages=args.messages,
                        timeout=10,
                        file=root / "payload.bin",
                    )
                )
            )
            result.update(
                {
                    "scope": "Single-process loopback: production server and clients; concurrent file/chat start; ACK means queued, not peer delivery.",
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "wall_seconds": monotonic() - started,
                    "combined_process_cpu_seconds": process_time() - cpu_started,
                }
            )
            if platform.system() == "Linux":
                import resource

                result["combined_process_peak_rss_kib"] = resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss
            from datetime import datetime, timezone

            result["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(
                f"Verified {result['file_bytes_verified']} bytes with {result['clients']} clients."
            )
            print(
                f"Chat ACK p95: {result['ack_p95_ms']:.1f} ms; PONG p95: {result['pong_p95_ms']:.1f} ms"
            )
            print(f"Raw results: {args.output}")
        finally:
            if server is not None:
                server.close()
            if thread is not None:
                thread.join(5)
            accounts.close()


if __name__ == "__main__":
    main()
