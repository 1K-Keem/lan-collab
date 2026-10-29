"""Opt-in TV3 real 20-client integration harness; requires TV1/TV2 complete.

NEVER labels mock coverage as real LAN evidence. Account input is local JSON
[{'username':'...', 'password':'...'}]; do not commit credentials.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep

from lancollab.client.__main__ import connect_verified_transport
from lancollab.client.application import ClientState, CollabClient


@dataclass(frozen=True)
class Metrics:
    clients: int
    chat_requests: int
    chat_ack_samples: int
    ack_mean_ms: float
    ack_p95_ms: float
    ack_max_ms: float
    file_bytes_verified: int | None
    scope: str = "Real TCP/TLS only if TV1/TV2 provided working adapters and server"


def _wait(predicate, *, seconds: float, context: str) -> None:
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise TimeoutError(context)


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    return data[max(0, (95 * len(data) + 99) // 100 - 1)]


def run(args: argparse.Namespace) -> Metrics:
    accounts = json.loads(Path(args.accounts).read_text(encoding="utf-8"))
    if not isinstance(accounts, list) or len(accounts) < args.clients:
        raise ValueError("Provide a separate account for every concurrent client")
    if len({item["username"] for item in accounts[: args.clients]}) != args.clients:
        raise ValueError("Account usernames must be unique to avoid takeover")
    data_dir = TemporaryDirectory(prefix="lancollab-smoke-")
    active: list[tuple[CollabClient, Event, Thread]] = []

    def connect(index: int) -> tuple[CollabClient, Event, Thread]:
        transport = connect_verified_transport(
            host=args.host, port=args.port, server_name=args.server_name, ca_file=args.ca_file
        )
        client = CollabClient(
            transport,
            auth_allowed=True,
            downloads_dir=Path(data_dir.name) / f"client{index}",
        )
        stop = Event()
        reader = Thread(target=client.receive_loop, args=(stop,), daemon=True)
        reader.start()
        deadline = monotonic() + 10
        try:
            req = client.send_hello(f"probe-{index}")
            del req
            _wait(
                lambda: client.state in {ClientState.WAIT_AUTH, ClientState.CLOSED},
                seconds=max(0.01, deadline - monotonic()),
                context="HELLO_ACK timeout",
            )
            if client.state is ClientState.CLOSED:
                raise ConnectionError("HELLO rejected")
            credentials = accounts[index]
            client.login(credentials["username"], credentials["password"])
            _wait(
                lambda: (
                    client.state
                    in {ClientState.AUTHENTICATED, ClientState.CLOSED, ClientState.WAIT_AUTH}
                ),
                seconds=max(0.01, deadline - monotonic()),
                context="AUTH_RESULT timeout",
            )
            if client.state is not ClientState.AUTHENTICATED:
                raise ConnectionError("AUTH failed or disconnected")
        except Exception:
            stop.set()
            client.close()
            raise
        return client, stop, reader

    try:
        with ThreadPoolExecutor(max_workers=min(args.clients, 32)) as pool:
            futures = [pool.submit(connect, i) for i in range(args.clients)]
            for future in as_completed(futures):
                active.append(future.result())
        samples = []
        with ThreadPoolExecutor(max_workers=min(args.clients, 32)) as pool:

            def send_many(item):
                client = item[0]
                durations = []
                for i in range(args.messages):
                    started = monotonic()
                    rid = client.send_global(f"TV3 smoke {i}")
                    _wait(
                        lambda: rid not in client._pending or client.state is ClientState.CLOSED,
                        seconds=args.timeout,
                        context="CHAT_ACK timed out",
                    )
                    if client.state is ClientState.CLOSED:
                        raise ConnectionError("Client disconnected during chat")
                    durations.append((monotonic() - started) * 1000)
                return durations

            for future in as_completed([pool.submit(send_many, item) for item in active]):
                samples.extend(future.result())
        verified = None
        if args.file:
            file_path = Path(args.file)
            if file_path.stat().st_size < 10 * 1024 * 1024:
                raise ValueError("CP-C workload requires >= 10 MiB file")
            sender, receiver = active[0][0], active[1][0]
            file_id = sender.offer_file(file_path, receiver.username)
            _wait(
                lambda: file_id in receiver.files.incoming,
                seconds=args.timeout,
                context="FILE_OFFER not relayed",
            )
            receiver.files.accept(file_id)
            _wait(
                lambda: file_id in sender.files.terminal and file_id in receiver.files.terminal,
                seconds=max(args.timeout, 60),
                context="FILE_RESULT/ACK incomplete",
            )
            # The actual directory uses connection index; locate the verified file safely.
            candidates = list(Path(data_dir.name).glob(f"client*/{file_path.name}"))
            if not candidates or all(x.read_bytes() != file_path.read_bytes() for x in candidates):
                raise AssertionError("Receiver file missing or hash differs")
            verified = file_path.stat().st_size
        return Metrics(
            clients=len(active),
            chat_requests=args.clients * args.messages,
            chat_ack_samples=len(samples),
            ack_mean_ms=mean(samples),
            ack_p95_ms=_percentile_95(samples),
            ack_max_ms=max(samples),
            file_bytes_verified=verified,
        )
    finally:
        for client, stop, reader in active:
            stop.set()
            client.close()
            reader.join(timeout=1)
        data_dir.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Real TV3 smoke; requires TV1 and TV2 integration")
    parser.add_argument("--accounts", required=True, help="LOCAL JSON; never commit secrets")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--server-name", default="lan-collab.local")
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--messages", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--file", help="Binary file >= 10 MiB for CP-C scenario")
    parser.add_argument("--output", default="tv3_smoke_result.json")
    args = parser.parse_args()
    if args.clients < 2 or args.messages <= 0 or args.timeout <= 0:
        parser.error("Use >=2 clients, positive messages and timeout")
    result = run(args)
    Path(args.output).write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
