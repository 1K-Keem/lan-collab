# Integration checkpoints — 25 Oct to 1 Nov 2026 (internal target)

Each checkpoint is a **gate**, not an exclusive phase. Merge small reviewed PRs as interfaces stabilize; checkpoints verify the assembled vertical slice and do not require long-lived per-person branches. All three members work in parallel with mocks and the revised contract. No gate can pass on in-memory mocks alone.

## CP-A — real TCP framing/handshake — 27 Oct, Issue #16

- [ ] TV1/TV2/TV3 acknowledge v1.2 changes in Issue #2; declare any blocked fields before merging handlers.
- [ ] Real server and client module commands connect with TCP; CP-A may exercise plaintext framing only with synthetic HELLO data on localhost. No AUTH_REQUEST/password is allowed until verified TLS works; basic TLS is a dependency of CP-B. Design bounded handshake workers/timeouts now.
- [ ] HELLO/HELLO_ACK with identical request_id and 4-byte length-framed JSON.
- [ ] Two clients connect concurrently; fragmented and combined frame tests pass.
- [ ] Codec follows declarative catalog and rejects unknown envelope/payload fields.
- [ ] Commit SHA, tests, logs and cross-team walkthrough linked to Issue #16.

## CP-B — 3-client auth/chat/UDP — 29 Oct, Issue #17

- [ ] Issue #10 basic verified TLS is integrated: trusted server identity, no CERT_NONE; only then enable AUTH_REQUEST.
- [ ] Admin provisions users; 3 accounts authenticate within HELLO/AUTH deadline.
- [ ] Three clients see correct atomic USER_LIST and online/offline updates with IP/ephemeral port.
- [ ] Global fan-out, private routing, CHAT_ACK request_id semantics and offline rejection work.
- [ ] 3 failed logins, timed-out silent client, stale-session takeover and safe terminal output tested.
- [ ] UDP DISCOVER/DISCOVER_REPLY and HEARTBEAT/HEARTBEAT_ACK measured, manual host fallback works.
- [ ] TCP PING/PONG remains authoritative liveness.
- [ ] Cross-team walkthrough and test logs attached to Issue #17.

## CP-C — file flow control/TLS/20 clients — 31 Oct, Issue #18

- [ ] Server-wide active UUID isolation, per-session terminal-ID reuse rejection and 1024-offer cap; simultaneous incoming/outgoing same-ID conflict tested. Transfers/events bind exact session IDs; takeover cancels old transfers without affecting new-session transfers. Receiver consent needed.
- [ ] W=8, cumulative ACK every 4 chunks and immediately at final bytes; END never waits for ACK drain. ACK accepted in VERIFYING, at actual chunk boundaries only; duplicate ACK gives no credit or timer progress. Success requires final ACK and receiver verified RESULT.
- [ ] FILE_END always follows chunks in same FIFO; no reordered finalization, including heavy control traffic.
- [ ] Saturated file queue cancels transfer without blocking peer reader or PONG; saturated control queue disconnects slow receiving session without silently dropping critical messages.
- [ ] Test 0 bytes; 1, 3, 4, 8, 9 chunks; partial final chunk; 50 MiB and limit+1; rejected/interrupted transfer, accept/verify timeout and malformed completion. No fsync requirement. Receiver verifies size/SHA-256, safe filename and no overwrite.
- [ ] Verified TLS, invalid cert rejected, encrypted payload in Wireshark, 5s TLS handshake deadline.
- [ ] 20 authenticated clients exchange global/private messages while a >=10 MiB file transfers; include one intentionally slow reader and kill/reconnect another client. Other clients continue, files match SHA-256 or explicitly fail/cancel, and server remains alive. Report CPU/RSS, failures and latency.
- [ ] On a normal localhost/LAN baseline (excluding the intentionally stalled peer), record >=100 chat ACK and TCP PING/PONG samples during transfer: p95 <=1s, maximum <5s, no unintended healthy-session disconnect. Record OS/network/file size and raw samples. These are internal project targets, not instructor-supplied thresholds; agree any environment-specific change before running the gate.
- [ ] Two-device LAN test; UDP cannot create authenticated session. Validate manual-IP fallback and test malformed UDP packets; RTT/loss display polish may follow the working file path.
- [ ] All evidence and limitations linked to Issue #18.

## Final handoff — 1 Nov, Issues #14/#15

- [ ] Real test + CI format/lint, clean clone, README, report diagrams, oral walkthrough.
- [ ] Ask lecturer if TLS alone satisfies encryption milestone; confirm submission visibility and deadline.
- [ ] Do not tag a checkpoint unless real socket integration and peer review pass.
