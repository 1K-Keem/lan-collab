# Production server connections and routing

Run from the project root after provisioning the certificate and first admin:

```bash
python -m lancollab.server --add-admin admin
python -m lancollab.server
```

See [TLS setup](tls-setup.md) for the matching `server-public.pem` and
`server-secret.pem`. Defaults: all IPv4 interfaces, TCP 40000, UDP 40001, 32
concurrent connections, including connections still completing TLS. Options include
`--port`, `--udp-port`, `--max-clients`, `--cert`, `--key` and `--db`.

## Runtime flow

1. Bind TCP and UDP synchronously; startup fails if either port is unavailable.
2. One UDP thread answers discovery and heartbeat probes. The main thread accepts TCP.
3. Admit a connection only if capacity is available. Its reader worker performs TLS
   with a five-second deadline. Authentication must finish within ten seconds after
   TLS, including partial-frame reads. Three invalid credentials close the connection.
4. Create one writer thread for that connection. Its mailbox holds at most 128 control
   frames and 32 file frames. Four controls then one file frame are scheduled when both
   queues have work. `FILE_END` stays behind preceding chunks in the file FIFO.
5. `CollabServer` serializes dispatch, registry changes and nonblocking queue admission.
   The actual socket writer never holds this shared lock. Presence snapshots, takeover,
   cancellation and forwarding therefore retain their application order.
6. Authentication opens a transfer-registry session. A new login for an existing
   username cancels the old session's transfers and closes its connection; stale cleanup
   cannot remove the replacement username mapping.
7. Chat is routed to exact authenticated sessions. File requests use `TransferRegistry`
   for consent, role checks, offsets, window credit, deadlines and cancellation. The
   server relays bounded file frames without saving files. Offline private messages
   are held in a bounded SQLite queue; long-term chat history is client-local.
8. Each connection tracks up to 65,536 request IDs without eviction. Duplicates are
   rejected without repeating operations. Matching PONG replies are exempt. Semantic
   validation errors produce a correlated ERROR; framing failures close the connection.
9. Every ten seconds the server probes authenticated clients with PING. Three unanswered
   probes cause closure at the following interval. UDP never changes online presence.
10. EOF, writer failure, queue overflow, timeout and shutdown invalidate the exact session
    and cancel its transfers. Slow control consumers disconnect; file queue overflow
    cancels the affected transfer with `QUEUE_FULL`. Shutdown joins reader/writer workers.

With 32 fully connected clients there can be 66 server application threads: main,
UDP, 32 readers and 32 writers. Workers are created on demand. The client also has its
own receiver, notice and file-sender workers; those are separate processes.

## Source map

| File | Responsibility |
| --- | --- |
| `server/__main__.py` | Arguments, first admin/password reset, database and server lifetime |
| `server/connections.py` | Accept, TLS workers, replay tracking, liveness, queue delivery |
| `server/outbound.py` | Bounded mailboxes, fair scheduling, purge and drain |
| `server/application.py` | Login/approval, session registry, presence, chat and transfer dispatch |
| `server/accounts.py`, `server/tokens.py` | SQLite password hashes and revocable token hashes |
| `server/queue.py` | Bounded, expiring offline private-message storage |
| `server/discovery.py` | UDP discovery and heartbeat responses |
| `common/transfer_core.py` | Transfer state, role authorization, credit and timers |

## Verification

```bash
python -m unittest tests.test_server_connections tests.test_auth_integration tests.test_production_server tests.test_transfer_integration -v
python -m scripts.simulate_clients --clients 20 --host 127.0.0.1 --send-auth
```

The simulation checks discovery/TLS and rejection of synthetic credentials, not successful
login. `test_production_server` and `test_transfer_integration` use the actual authenticated
server for chat, files, takeover and failures. The old unauthenticated MiniRelay has been
removed. [Local benchmark](local-benchmark.json) records a separate loopback workload;
[acceptance instructions](acceptance.md) describe the remaining two-host evidence.
