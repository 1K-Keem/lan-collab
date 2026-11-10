# Concurrent server connection milestone

Run from the project root:

```bash
python -m lancollab.server
```

Defaults: all local IPv4 interfaces, TCP 40000, UDP 40001, and 32 active connection
workers. Configure with `--port`, `--udp-port`, `--max-clients`, `--cert`, and `--key`.
Port 0 is useful in tests: discovery advertises the actual assigned TCP port.

## Ownership and flow

- `server/__main__.py`: parses options, loads the TLS context, starts ConnectionServer.
- `server/connections.py`: TCP accept loop, capacity control, workers and shutdown.
- `server/discovery.py`: validates DISCOVER and returns DISCOVER_REPLY over UDP.
- `server/application.py`: remains an unimplemented authentication/routing boundary.

TCP and UDP sockets are bound synchronously: a UDP bind error fails startup rather than
silently leaving only TCP running. One UDP worker responds while the main thread accepts
TCP connections. Each accepted connection gets a worker that performs the bounded TLS
handshake. Up to 32 workers are admitted by default, including peers still handshaking;
additional connections close without an unencrypted protocol reply. This is a concurrency
limit, not the TCP listen backlog. Workers are created on demand, not all at startup.
With 32 clients there are 34 application threads: main, UDP, and 32 connection workers.

No network handshake is performed under the worker registry lock. A silent handshake peer
has a 5-second default deadline and does not block other handshakes or UDP discovery.
Each verified connection waits up to 10 seconds for a first message or peer disconnect.
A first application frame is logged by type only and the connection is closed: this stage
does not authenticate credentials, send AUTH_RESULT, or route chat/file messages. It is not
a complete implementation of the protocol's authentication state machine. Partial frames
remain subject to the transport's own absolute frame-read deadline.

Ctrl+C requests shutdown, closes sockets/transports and joins the workers. TLS handshakes
already in progress remain bounded by their handshake deadline. UDP receives use a short
timeout to notice shutdown; receive failures stop the TCP accept loop as well.

## Exercise the milestone

Keep the server running and, in another terminal, run:

```bash
python -m scripts.discover
python -m scripts.handshake_client
python -m scripts.handshake_client
```

The discovery script prints the endpoint; each handshake client connects and exits. Unlike
the standalone `scripts.handshake_server` demo, this server keeps accepting new clients.
The full `lancollab.client` still cannot log in until authentication is implemented.
Standalone UDP is also available with `python -m lancollab.server.discovery`; do not run
it on the same UDP port as the combined server. Heartbeat replies remain future work.

## Verification

```bash
python -m unittest discover -s tests -p test_server_connections.py -v
```

Real loopback tests hold 32 TLS connections open simultaneously, exercise discovery while
all slots are occupied, reject a 33rd connection, release/reuse capacity, verify that a
silent handshaking peer does not block healthy clients, test UDP bind failure and idle
cleanup, and verify shutdown. This is local connection evidence, not 20-user chat/file
acceptance or two-machine LAN evidence.

## Detailed client simulation against your running server

```bash
python -m scripts.simulate_clients
python -m scripts.simulate_clients --clients 20
python -m scripts.simulate_clients --clients 20 --host 127.0.0.1
python -m scripts.simulate_clients --send-auth
```

The script defaults to discovery. Each simulated client has its own UDP request identifier,
TCP socket and TLS handshake. Timestamped logs show discovery results, local/remote TCP
addresses, verified hostname, negotiated TLS version/cipher, and a diagnostic certificate
fingerprint. All established connections are observed concurrently for two seconds, then
closed. `--hold` changes the observation time (at most five seconds to stay below the
milestone's idle deadline). Discovery/TLS still take time; a stalled batch can cause early
connections to reach the server's deadline, which the script reports as a failure.

Use `--discovery-address 127.0.0.1` to test UDP without broadcast on the same machine;
`--host` skips UDP entirely. Configure ports with `--port`/`--udp-port` and trust with
`--trusted-cert`. The script uses real sockets, not FakeTransport, and does not start a
server. Its logs describe API results, not packet-level SYN/SYN-ACK capture.

Optional `--send-auth` sends synthetic credentials and expects the current server to close
without AUTH_RESULT. This demonstrates the unfinished authentication boundary, not a
successful login; never supply a real password for this milestone. The process returns 0
only when all requested client checks pass, otherwise 1. Over-capacity clients may fail
TLS as expected, but still count as failures in this script's summary. A successful run
is not evidence of chat/file support or internal server thread counts.
