# LAN Collab

Secure, self-hosted LAN collaboration application for a **Network Application Development** assignment using **raw TCP/UDP sockets**.

> **Status: CONTRACT + DUMMY SCAFFOLD ONLY.** Authentication, networking, routing, file transfer and encryption are **not implemented**. Do not interpret green CI or mock examples as working application functionality.

## Requirements and scope

- Python **3.11+**, raw `socket`, `threading`, `ssl` for TCP/TLS; UDP discovery/presence/heartbeat.
- Login, online users, global and private chat, 20 simultaneous clients.
- Reliable file relay with chunking, SHA-256 verification and cancellation.
- Timeouts, error handling, reconnect, and a clear CLI.
- No Flask/Django/HTTP networking frameworks/WebSockets.

See [assignment checklist](docs/assignment-checklist.md).

## Team roles (assign GitHub usernames later)

| Role | Responsibilities |
| --- | --- |
| TV1 | Server listener, session/auth state, concurrency and message routing |
| TV2 | Shared protocol/framing, TLS certificate integration, file relay |
| TV3 | Client/CLI, UDP discovery/heartbeat and integration testing |

All team members review protocol changes and cross-review the full codebase for oral defense.

## Layout

```text
lancollab/
  common/                # Typed contracts, FakeTransport + TV2 real transport TODO
  server/                # TV1 server handler and module entry point TODO
  client/                # TV3 client API and module entry point TODO
tests/                   # Mock-only tests (real socket tests not yet written)
scripts/                 # Mock-only smoke scenario
docs/                    # Architecture, frozen v1 baseline, checkpoints, report
.github/workflows/       # CI
```

## Protocol diagrams

GitHub renders the Mermaid diagrams directly in these Markdown files.

- [Transport and architecture overview](docs/diagrams/transport-overview.md)
- [TCP framing and JSON envelope](docs/diagrams/tcp-framing.md)
- [Connection, authentication and chat](docs/diagrams/connection-and-chat.md)
- [File transfer sequence and state machine](docs/diagrams/file-transfer.md)
- [Outbound queue fairness and backpressure](docs/diagrams/outbound-queues.md)
- [Parallel work dependencies and checkpoints](docs/diagrams/parallel-work-dependencies.md)

**Protocol status:** v1.0 frozen as implementation baseline by repository owner; individual TV1/TV2/TV3 reviews still need recording in [Issue #2](https://github.com/1K-Keem/lan-collab/issues/2). This does not imply that the application is implemented.

## Development setup

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

`python -m lancollab.server` and `python -m lancollab.client` **intentionally fail with "not implemented"** until TV1 and TV3 complete the work. Update this section with actual run commands after implementation.

## Work in parallel (one week)

1. **All three** review [protocol contract](docs/protocol-spec.md) and [architecture](docs/architecture.md) and approve Issue #2.
2. **TV1** implements server handlers against the typed API and `FakeTransport` without depending on a real client.
3. **TV2** implements length-prefixed TCP codec, TLS/SSL wrapping, file chunking and trust setup without depending on the real server handlers.
4. **TV3** implements client/CLI using `FakeTransport` and synthetic server messages, plus UDP discovery and tests.
5. Integrate and check [checkpoint gates](docs/checkpoints.md): **CP-A** (27 Oct), **CP-B** (29 Oct), **CP-C** (31 Oct), final packaging (1 Nov 2026).

Read [mock development guide](docs/mock-development.md) for example interfaces. Keep changes in short-lived PRs, with meaningful tests and attribution. The deadline in the assignment is only "suggested" and contradictory; the above is the team's **internal one-week target**.

## TLS and local LAN

The final application must verify server identity with trusted certificates. Project plan: a certificate-generation helper will be added by TV2 and documented for Windows/macOS/Linux; no private keys should be committed. Use manual `--host` fallback when UDP broadcast is blocked.

## Submission and academic integrity

Deliver source, exact run commands, architecture/protocol/file/message diagrams, testing evidence, and report PDF. Instructor rules restrict AI to syntax/debugging assistance and require team members to explain **100% of the codebase**. Repository placeholders are coordination aids, not completed graded work.
