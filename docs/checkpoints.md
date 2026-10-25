# Checkpoints — one-week MVP (25 Oct–1 Nov 2026)

Checkpoints are **integration gates**, not separate development phases. Members code simultaneously using the fixed contract and mocks. The assignee of a feature provides proof; the other two review. Mark gate complete only after real network tests.

## CP-A — Transport integration — target 27 Oct (Issue #16)

**Depends on:** #2 proposed protocol signed off; #3 framing; #4 TCP server; #7 TCP client (minimal).

**Must demonstrate:**
- [ ] `python -m lancollab.server` starts a real TCP server; client launches and connects on localhost.
- [ ] A verified TLS connection if available, or explicitly mark TLS pending and track #10 as a blocking task for CP-C.
- [ ] HELLO -> HELLO_ACK round trip using shared frame encoding; fragmented/coalesced frames tested.
- [ ] Two clients connect concurrently without blocking accept loop.
- [ ] Test commands/logs linked in Issue #16; all three can explain the framing code.

**Not met by:** `scripts/mock_contract_demo.py`; it only tests synthetic in-memory messages.

## CP-B — Multi-client auth/chat/presence — target 29 Oct (Issue #17)

**Depends on:** CP-A + #5 auth + #6 routing + #7 CLI + basic #9 discovery.

**Must demonstrate:**
- [ ] Three clients authenticate with distinct users.
- [ ] Online list updates on connect and disconnect.
- [ ] Global chat reaches other online clients.
- [ ] Private chat only reaches intended recipient and ACK semantics documented.
- [ ] TCP liveness and basic UDP discovery/manual-IP fallback demonstrated.
- [ ] Invalid auth, offline user and unexpected exit handled without killing server.

## CP-C — End-to-end acceptance — target 31 Oct (Issue #18)

**Depends on:** CP-B + #8 file transfer + #10 TLS + #11 resilience + #12 tests + #13 CLI progress.

**Must demonstrate:**
- [ ] Receiver offers/accepts file; receives binary bytes intact with matching size and SHA-256.
- [ ] Chat/control still responsive while file is in progress; bounded queue/backpressure behavior observed.
- [ ] Interrupting sender/receiver doesn't crash server; transfer ends FAILED or CANCELLED and cleans temp data.
- [ ] Certificate validation actually enforced; capture shows encrypted application payload.
- [ ] UDP beacon/heartbeat present, but cannot spoof authenticated presence.
- [ ] Automated 20-client run with outcome log; at least two physical devices tested over LAN.
- [ ] Report diagrams match implemented behavior; unfinished requirements are flagged.

## Final packaging — target 1 Nov (Issues #14, #15)

- [ ] Run CI format+lint+tests, clean clone run, and demo walkthrough.
- [ ] Report PDF includes architecture, protocol, 2 sequence diagrams and challenges/solutions.
- [ ] Confirm submission rules with lecturer; tag `checkpoint-a`, `checkpoint-b`, `checkpoint-c` only when each is verified.
- [ ] Cross-review: each member can explain critical paths outside their main module.

## Definition of evidence

Every checkpoint Issue comment should include: commit SHA, test command, environment (OS, host IP modes), observed results and known failures. A green scaffold CI run is not evidence for a milestone. Prefer short PRs and integrate daily.
