# Reproducing integration and acceptance evidence

## Automated checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m ruff check .
```

The production integration tests cover 20 logged-in users, concurrent chat during a
10 MiB transfer, ten simultaneous sender/receiver pairs, empty/small files, cancellation,
disconnect/reuse, takeover, replay rejection, malformed requests, authentication deadlines,
heartbeat cleanup and deliberately stalled writers. Core tests cover chunk boundaries,
window violations, hash/filename failures and transfer deadlines. Client tests cover local
history isolation, remembered login failures and shared CLI/TUI command dispatch.

`docs/local-benchmark.json` contains an actual WSL loopback run on 2026-11-10: 20 clients,
100 chat ACK samples, 100 TCP PONG samples, and a verified 10 MiB file. File and chat
workloads start concurrently. The samples are application-level timings and include
scheduling; ACK means queued rather than recipient delivery. They do not constitute a
second-machine LAN run or server-only resource profile.

Reproduce the isolated local run (temporary accounts/certificates; no real server changes):

```bash
python -m scripts.local_acceptance --output data/local-benchmark.json
```

The resource figures in this local run describe the combined server/client process.

## Running the workload against another machine

Prepare at least 20 approved test accounts through registration/admin approval. Store their
synthetic credentials in a local `data/accounts.json` file, which is ignored by Git:

```json
[{"username": "user0", "password": "your-test-password"}]
```

The real file must contain one distinct entry per simulated client. Prepare a non-sensitive
file of at least 10 MiB, then run on the client machine:

```bash
python -m scripts.tv3_real_smoke --accounts data/accounts.json --host SERVER_IP --trusted-cert lancollab/client/server-public.pem --clients 20 --messages 5 --file data/sample10m.bin --output data/lan-results.json
```

The result includes raw chat ACK and PONG timings, mean/p95/max and verified file size.
Record OS, network type, hardware and server CPU/RSS separately. Internal baseline targets
are p95 <=1 s and maximum <5 s; these are project targets, not instructor requirements.

## Manual evidence still required

- Two physical hosts: broadcast discovery, manual-IP fallback, login and bidirectional files.
- On the actual target terminals, exercise TUI resizing, scrolling and Unicode input; use CLI
  on platforms without curses. Check terminal output during incoming messages.
- Wireshark capture: TCP handshake, TLS negotiation and encrypted application records;
  UDP discovery/heartbeat. Never put passwords or private keys into the report.
- Record resource usage and failure/reconnect observations on the deployment machines.
- Team review, instructor encryption-rubric clarification and final submission details.

The implementation report is a draft describing code and measured local evidence. Add
these manual results and the team's own explanation before submitting it.
