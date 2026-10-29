# TV3 implementation, team boundaries and reporting index

**Date:** 2026-10-29. **Wire protocol:** version 1, documentation revision 1.2.

## Implemented (TV3-owned)

- `lancollab/client/application.py`: typed Envelope message lifecycle, HELLO/AUTH, global/private chat, online snapshot/presence, PING/PONG, EOF/cleanup, file event routing and correlation for server ERROR.
- `lancollab/client/cli.py`: portable `/help`, `/users`, `/status`, `/all`, `/msg`, `/send`, `/accept`, `/reject`, `/cancel`, `/net`, `/quit` commands and separate receive/notice worker.
- `lancollab/client/__main__.py`: CLI arguments, credentials prompted before TLS connect, discovery/manual-host selection and mandatory verified TLS connector interface; **fails closed** while TV2 implementation is missing.
- `lancollab/client/discovery.py`: ephemeral UDP socket, DISCOVER/DISCOVER_REPLY matching, heartbeat seq/RTT/loss and `--host` fallback. UDP has zero auth/presence authority.
- `lancollab/client/transfers.py`: offer SHA256, user consent, worker streaming chunks after acceptance, 8 outstanding window and cumulative ACK, temp receiver writes, chunk order/size validation, final SHA256, atomic no-overwrite name and terminal cleanup. Only FILE_RESULT(true) is success.
- `tests/test_client_logic.py`, `test_client_cli.py`, `test_client_discovery.py`, `test_client_transfers.py`, `test_client_file_relay_mock.py`, `test_client_load_mock.py`: deterministic local tests; **mock evidence**.
- `scripts/tv3_real_smoke.py`: opt-in real transport acceptance and chat latency collector; it is **not** a claim that CP-C has run.

## Explicit dependency boundaries (not implemented by TV3)

1. TV2: `SocketTransport.connect_verified(host, port, server_name, ca_file)` must establish hostname-verified TLS before returning a `MessageTransport`. TV2 also owns real TCP length-prefix framing and strict JSON schema validation; TV3 never falls back to plaintext TCP.
2. TV1: server-side login/auth, chat/presence routing, UDP DISCOVER_REPLY/HEARTBEAT_ACK, queues, file offer/accept/ACK/RESULT relay and session takeover/cleanup.
3. TV2: common transfer-core protocol/credit validation (including server-side window and UUID/session authorization), coordinated with TV1's relay; this client module does not replace that core.
4. Real 3-client and two-host LAN evidence, 20 concurrent clients, 10 MiB simultaneous file/chat workload, CPU/RSS and 100+ latency samples **pending until server/transport are available**.

## Reproduce local mock tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q lancollab tests scripts
ruff check .
ruff format --check .
```

## Real CLI after TV1/TV2 integration

```bash
python -m lancollab.client --host 192.0.2.10 --port 40000 --server-name lan-collab.local --ca-file path/to/trusted-ca.pem --downloads-dir downloads
python -m lancollab.client --discover --server-name lan-collab.local --ca-file path/to/trusted-ca.pem
```

- `--discover` requires a TV1 UDP responder; use explicit `--host` when UDP broadcast is blocked.
- `--server-name` remains the trusted TLS identity even if host/IP is discovered dynamically; the username/password are collected before TCP/TLS to meet deadline.
- Until TV2 implements the verified TLS connector, the application raises a clear error instead of silently sending a password over plaintext TCP.

## Real workload harness after TV1/TV2 integration

Create a **local untracked file** `accounts.local.json` containing 20 unique server-provisioned accounts, e.g. `[ {"username":"user1","password":"***"}, ... ]`. Never commit it or paste real secrets into reports. Prepare a non-sensitive >= 10 MiB test file.

```bash
python -m scripts.tv3_real_smoke --accounts accounts.local.json --host 192.0.2.10 --server-name lan-collab.local --ca-file path/to/trusted-ca.pem --clients 20 --messages 5 --file data/sample10m.bin --output tv3_smoke_result.json
```

The resulting report provides queued-chat ACK timing and file equality if completed. These metrics **do not measure peer delivery/read latency**, and the run itself does not prove PONG latency, CPU/RSS or two-host LAN coverage; add real measurements separately before CP-C.

## Report-ready diagrams

- [Client state](diagrams/client-issue7-state.md)
- [Request correlation](diagrams/client-issue7-requests.md)
- [Concurrency boundary](diagrams/client-issue7-concurrency.md)
- [UDP probes](diagrams/client-udp-probes.md)
- [Client file pipeline](diagrams/client-file-transfer.md)
- [Test and evidence gates](diagrams/client-test-matrix.md)

## Checklist before merge/release

- [x] TV3 code organized behind MessageTransport; no modifications to TV1/TV2 implementations
- [x] Local deterministic tests for chat, UDP, file/ACK and multi-client fake
- [ ] TV2 verified TLS connector + server deployed, 3-client runtime evidence
- [ ] Real UDP LAN RTT/loss and fallback evidence
- [ ] Real file transfer 0, 1, 3, 4, 8, 9 chunks and 10 MiB; actual SHA/ACK logs
- [ ] Real 20-client CP-C 100+ latency samples and CPU/RSS
- [ ] Reviewer signoff, Notion explanations, oral demo, report figures verified rendered

**Status:** TV3 implementation in PR is ready for integration review; project acceptance is pending other members and real network tests.