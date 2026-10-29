# LAN Collab

Distributed LAN client-server collaboration application for a Computer Networking assignment. Networking must use raw TCP/UDP sockets (no Flask/HTTP/WebSocket networking layer).

> **Status:** TV3 client message logic, CLI, UDP probes and client-side file I/O are implemented and **mock-tested** (PR #21). TV1 server, TV2 verified TLS/codec/core and real LAN integration remain pending. Green CI does **not** imply acceptance of real networking milestones.

## Target system features and integration dependencies

- TLS-protected TCP login, global/private messages, server presence list, 20 concurrent clients.
- Admin-provisioned user accounts, secure salted password hashes, HELLO/AUTH deadlines.
- Session-bound transfers/events, globally unique active transfer IDs and explicit takeover cancellation.
- Reliable streaming server file relay with **FILE_ACK receiver credit** (window 8 × 16KiB), SHA-256, safe filenames and nonblocking queues.
- UDP broadcast discovery + unicast HEARTBEAT/HEARTBEAT_ACK RTT/loss estimates; manual host/IP fallback.
- CLI with safe text rendering, file progress and timeout/disconnect handling.

## Team roles and independent development

| Role | Owns | Can start without other implementations |
| --- | --- | --- |
| TV1 | Server, sessions/auth, chat routing, bounded queues | In-memory fake transport + protocol schema |
| TV2 | Codec/TLS and socket-independent transfer state/ACK/security core | Schema fixtures, codec and chunk tests |
| TV3 | Client/CLI, file consent/disk I/O/streaming, UDP, integration testing | Fake server messages and mocked event streams |

**Protocol:** [Design revision 1.2](docs/protocol-spec.md) supersedes v1.1, pending individual TV1/TV2/TV3 reviews in [Issue #2](https://github.com/1K-Keem/lan-collab/issues/2). The wire envelope `version` remains **1** (not 1.2). Do not merge incompatible handlers until the team confirms the contract.

## Layout

```text
lancollab/common/   # Typed contracts, message_schema.py, test dummy, TV2 transport TODO
lancollab/server/   # TV1 server TODO and main CLI
lancollab/client/   # TV3 client logic, CLI, discovery and file I/O; TV2 TLS adapter still pending
tests/              # Synthetic and unit/mock tests, NOT production/LAN proof
scripts/            # Mock demo plus opt-in real smoke harness (requires TV1/TV2)
docs/               # Architecture, protocol spec, diagrams, checkpoint gates
```

## Developer setup (mock-only now)

```bash
git clone https://github.com/1K-Keem/lan-collab.git
cd lan-collab
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py"
python -m scripts.mock_contract_demo
python -m lancollab.server --help
python -m lancollab.client --help
```

The server and admin account creation commands are still TV1 TODOs. The TV3 client CLI now parses commands and UDP/discovery options, but `python -m lancollab.client` deliberately **fails closed** until TV2 implements `SocketTransport.connect_verified` with verified TLS. Never use real credentials with FakeTransport or plaintext TCP.

## TV3 implementation and report handoff

- [TV3 implementation and current integration limits](docs/tv3-implementation.md)
- [Client state diagram](docs/diagrams/client-issue7-state.md)
- [Request correlation diagram](docs/diagrams/client-issue7-requests.md)
- [Concurrent client worker diagram](docs/diagrams/client-issue7-concurrency.md)
- [UDP probe diagram](docs/diagrams/client-udp-probes.md)
- [Client file transfer diagram](docs/diagrams/client-file-transfer.md)
- [Test coverage and gates](docs/diagrams/client-test-matrix.md)

## Architecture and diagrams (render on GitHub)

- [Transport overview](docs/diagrams/transport-overview.md)
- [TCP length-prefix JSON](docs/diagrams/tcp-framing.md)
- [Connection, global/private chat and liveness](docs/diagrams/connection-and-chat.md)
- [FILE_ACK window, file states and sequence](docs/diagrams/file-transfer.md)
- [Queue backpressure and FIFO ordering](docs/diagrams/outbound-queues.md)
- [Parallel tasks and integration gates](docs/diagrams/parallel-work-dependencies.md)
- [Integration checkpoints CP-A/B/C](docs/checkpoints.md)
- [Mocks/contract documentation](docs/mock-development.md)

## Workflow and academic integrity

Contribute through short-lived branches/PRs with real authors and tests. Cross-review full codebase before oral defense. The assignment limits AI assistance to syntax references and debugging: mock scaffolding/documentation does **not** replace student-implemented networking logic. Clarify with lecturer whether TLS suffices for the encryption rubric and whether public/private repository submission is required.
