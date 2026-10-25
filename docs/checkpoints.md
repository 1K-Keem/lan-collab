# Integration checkpoints — 25 Oct to 1 Nov 2026 (internal target)

Each checkpoint is a **gate**, not an exclusive phase. All three members work in parallel with mocks and the revised contract. No gate can pass on in-memory mocks alone.

## CP-A — real TCP framing/handshake — 27 Oct, Issue #16

- [ ] TV1/TV2/TV3 acknowledge v1.1 changes in Issue #2; declare any blocked fields before merging handlers.
- [ ] Real server and client module commands connect with TCP; TLS integration can finish by CP-C, but handshake threading and timeouts must be designed now.
- [ ] HELLO/HELLO_ACK with identical request_id and 4-byte length-framed JSON.
- [ ] Two clients connect concurrently; fragmented and combined frame tests pass.
- [ ] Codec follows declarative catalog and rejects unknown envelope/payload fields.
- [ ] Commit SHA, tests, logs and cross-team walkthrough linked to Issue #16.

## CP-B — 3-client auth/chat/UDP — 29 Oct, Issue #17

- [ ] Admin provisions users; 3 accounts authenticate within HELLO/AUTH deadline.
- [ ] Three clients see correct atomic USER_LIST and online/offline updates with IP/ephemeral port.
- [ ] Global fan-out, private routing, CHAT_ACK request_id semantics and offline rejection work.
- [ ] 3 failed logins, timed-out silent client, stale-session takeover and safe terminal output tested.
- [ ] UDP DISCOVER/DISCOVER_REPLY and HEARTBEAT/HEARTBEAT_ACK measured, manual host fallback works.
- [ ] TCP PING/PONG remains authoritative liveness.
- [ ] Cross-team walkthrough and test logs attached to Issue #17.

## CP-C — file flow control/TLS/20 clients — 31 Oct, Issue #18

- [ ] File roles enforced against impersonation, UUID conflict rejected, receiver consent needed.
- [ ] Sender window W=8, receiver cumulative FILE_ACK every 4 chunks/final partial, progress measured by ACK next_offset.
- [ ] FILE_END always follows chunks in same FIFO; no reordered finalization, including heavy control traffic.
- [ ] Saturated file queue cancels transfer without blocking peer reader or PONG; saturated control queue disconnects slow receiving session without silently dropping critical messages.
- [ ] Empty file, binary file, rejected offer, interrupted transfer, post-accept timeout and verify timeout handled. Receiver verifies size/SHA-256, safe filename and no overwrite.
- [ ] Verified TLS, invalid cert rejected, encrypted payload in Wireshark, 5s TLS handshake deadline.
- [ ] 20 clients with recorded outcome; two-device LAN test; UDP can't create authenticated session.
- [ ] All evidence and limitations linked to Issue #18.

## Final handoff — 1 Nov, Issues #14/#15

- [ ] Real test + CI format/lint, clean clone, README, report diagrams, oral walkthrough.
- [ ] Ask lecturer if TLS alone satisfies encryption milestone; confirm submission visibility and deadline.
- [ ] Do not tag a checkpoint unless real socket integration and peer review pass.
