# Architecture and parallel development decisions

**Status:** Protocol v1.0 frozen as an implementation baseline by repository owner (25 Oct 2026). Each teammate must still review/acknowledge Issue #2; diagrams describe intended behavior, not completed code.

## Server-mediated collaboration (no direct P2P)

```text
Client A -- TCP/TLS control+file --+
Client B -- TCP/TLS control+file --+ --> Single Server: auth/session, chat routing, file relay
Client C -- TCP/TLS control+file --+
          UDP broadcast discovery/presence and lightweight heartbeat
```

The server never stores relayed files. Sender and receiver must both be online and receiver must accept first.

## Package boundaries (stable on main)

```text
lancollab/common/  # TV2: typed envelope, protocol codec, transport contract
lancollab/server/  # TV1: TCP accept, sessions, auth, routing; TV2 integrates file relay/TLS
lancollab/client/  # TV3: client connection, CLI, UDP discovery
tests/             # TV3 leads; unit and integration tests owned by implementers
scripts/           # test/dummy helpers (not production networking)
docs/              # versioned decisions, checkpoints and report
```

Run entry points (after implementation): `python -m lancollab.server`, `python -m lancollab.client`. Their current placeholder CLI only prints not-yet-implemented; this is intentional.

## How three members start in parallel

1. Read the owner-frozen v1 contract in `docs/protocol-spec.md`, fixtures, `MessageTransport` and ACK semantics; record individual review in #2.
2. TV1 creates socket listener and server handlers against `MessageTransport` using `FakeTransport` and fixture messages. **Do not wait** for TV2's real codec.
3. TV2 implements real length-prefix JSON codec and TLS wrapper, plus file chunking/hash tested on in-memory bytes/temporary files.
4. TV3 builds client/CLI/UDP and contract tests against `FakeTransport` or scripted peer. **Do not wait** for a real server.
5. Replace dummy adapter with actual TCP/TLS transport at checkpoint A; integrate once per day, keep short PRs.

All shared contract changes require approval from all three; do not hand-edit wire schemas independently.

## Decisions for one-week scope

- Python 3.11+, raw sockets, Python threading, `ssl`, JSON + length-prefix, SQLite for account storage.
- TLS before application login. Cert-generation script cross-platform; dev cert trusted locally, no bundled private keys.
- Single TLS/TCP connection per client. Two bounded queues (control first, file fair scheduling), 16 KiB raw file chunks; backpressure on full file queue, no unbounded buffering.
- UDP discovery + presence beacon/heartbeat best-effort; authoritative online status from authenticated TCP.
- File transfer offer->accept->chunk->end->result; SHA-256 and size verification, temp files and sanitized basenames.
- CLI first; no GUI, chat history, E2EE, multi-server or resumable transfer.
- Integration check after every major merge; tags only after a checkpoint actually passes.

## Dependency map

```text
Protocol v1.0 baseline (#2) ───> real codec (#3) ───┬──> server (#4,#5,#6)
            ├──> TV1 server skeleton (FakeTransport)│
            ├──> TV3 client skeleton (FakeTransport)└──> client (#7)
            ├──> TV2 file/TLS offline tests ──────────> end-to-end #8/#10
            └──> TV3 UDP + test harness ──────────────> #9/#12

       Checkpoint A: TCP/TLS handshake integration
               ↓
       Checkpoint B: 3-client auth/chat/presence
               ↓
       Checkpoint C: file/TLS/resilience/20 clients
               ↓
       README / report / oral walkthrough
```

## Known technical risks

- TLS handshake must be in a worker, not in accept loop (slow peers otherwise block new connections).
- Every connection must serialize full frames; cannot assume `recv(n)` yields `n`.
- Sender's file traffic cannot monopolize control frames; queue fullness must have defined handling.
- UDP can be blocked; always provide manual server address.
- `input()` can collide visually with incoming events; TV3 decides CLI display strategy early (e.g., `prompt_toolkit`).
- CI green on dummy tests **does not** mean assignment is implemented.

## Instructor clarifications

Assignment itself inconsistently specifies 6 vs 8 weeks, a suggested 2026-11-17 deadline with wrong weekday, and public vs private repository submission. Internal schedule is one week from 2026-10-25 to 2026-11-01 unless changed by team.

## Visual architecture and workflows

- [Transport overview](diagrams/transport-overview.md)
- [TCP framing](diagrams/tcp-framing.md)
- [Connection, authentication and chat](diagrams/connection-and-chat.md)
- [File transfer sequence and state machine](diagrams/file-transfer.md)
- [Outbound queues and fairness](diagrams/outbound-queues.md)
- [Parallel implementation dependencies](diagrams/parallel-work-dependencies.md)
