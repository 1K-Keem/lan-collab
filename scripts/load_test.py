"""Opt-in 20-client production-server chat/file workload.

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

from lancollab.common.socket_transport import SocketTransport
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
    pong_mean_ms: float
    pong_p95_ms: float
    pong_max_ms: float
    chat_ack_raw_ms: list[float]
    pong_raw_ms: list[float]
    scope: str = "Real TCP/TLS to the selected host; chat ACK means queued, not read"


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
        transport = SocketTransport.connect_verified(
            host=args.host,
            port=args.port,
            server_hostname=args.server_name,
            trusted_cert=args.trusted_cert,
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
            failures = []
            for future in as_completed(futures):
                try:
                    active.append(future.result())
                except Exception as error:
                    failures.append(error)
            if failures:
                raise failures[0]
        file_id = None
        if args.file:
            file_path = Path(args.file)
            if file_path.stat().st_size < 10 * 1024 * 1024:
                raise ValueError("File workload requires >= 10 MiB file")
            sender, receiver = active[0][0], active[1][0]
            file_id = sender.offer_file(file_path, receiver.username)
            _wait(
                lambda: file_id in receiver.files.incoming,
                seconds=args.timeout,
                context="FILE_OFFER not relayed",
            )
            receiver.files.accept(file_id)
        samples = []
        pong_samples = []
        with ThreadPoolExecutor(max_workers=min(args.clients, 32)) as pool:

            def send_many(item):
                client = item[0]
                durations = []
                pongs = []
                for i in range(args.messages):
                    started = monotonic()
                    rid = client.send_global(f"Load test {i}")
                    ping_id = client.ping()
                    _wait(
                        lambda: rid not in client._pending or client.state is ClientState.CLOSED,
                        seconds=args.timeout,
                        context="CHAT_ACK timed out",
                    )
                    if client.state is ClientState.CLOSED:
                        raise ConnectionError("Client disconnected during chat")
                    durations.append((monotonic() - started) * 1000)
                    _wait(
                        lambda: ping_id in client.ping_rtts or client.state is ClientState.CLOSED,
                        seconds=args.timeout,
                        context="PONG timed out",
                    )
                    if client.state is ClientState.CLOSED:
                        raise ConnectionError("Client disconnected awaiting PONG")
                    pongs.append(client.ping_rtts.pop(ping_id))
                return durations, pongs

            for future in as_completed([pool.submit(send_many, item) for item in active]):
                chats, pongs = future.result()
                samples.extend(chats)
                pong_samples.extend(pongs)
        verified = None
        if file_id is not None:
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
            pong_mean_ms=mean(pong_samples),
            pong_p95_ms=_percentile_95(pong_samples),
            pong_max_ms=max(pong_samples),
            chat_ack_raw_ms=samples,
            pong_raw_ms=pong_samples,
        )
    finally:
        for client, stop, reader in active:
            stop.set()
            client.close()
            reader.join(timeout=1)
        data_dir.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Production server concurrent chat/file smoke")
    parser.add_argument("--accounts", required=True, help="LOCAL JSON; never commit secrets")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=40000)
    parser.add_argument("--server-name", default="lan-collab.local")
    parser.add_argument("--trusted-cert", required=True)
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--messages", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--file", help="Binary file >= 10 MiB for the file workload")
    parser.add_argument("--output", default="data/load-results.json")
    args = parser.parse_args()
    if args.clients < 2 or args.messages <= 0 or args.timeout <= 0:
        parser.error("Use >=2 clients, positive messages and timeout")
    result = run(args)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
