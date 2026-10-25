# Architecture and parallel development — revision 1.1

**Status:** Updated baseline proposal after protocol review on 25 Oct 2026. Team acknowledgments remain pending in Issue #2.

## Components and transport

```text
Client Alice ---- TCP/TLS ----+
Client Bob ------ TCP/TLS ----+--- Central server
Client Charlie -- TCP/TLS ----+     session/auth and presence registry
             UDP discovery -->     chat routing + bounded file streaming relay
           UDP HEARTBEAT -->       (no server-side file persistence)
```

- TCP/TLS: 4-byte unsigned big-endian length, UTF-8 JSON v1 envelope; control messages and file chunks use one serialized writer per connection.
- UDP: `DISCOVER/DISCOVER_REPLY` and `HEARTBEAT/HEARTBEAT_ACK` (client UDP ephemeral source port). UDP measures discovery and network quality; TCP PING/PONG determines online status.
- Single server; no HTTP/WebSocket, direct P2P, chat history, file resume, multiple data sockets or E2EE.
- Main CLI package entry points (after implementation): `python -m lancollab.server` and `python -m lancollab.client`.

## Package owners and independent starts

| TV | Owned implementation | Independent starting surface |
| --- | --- | --- |
| TV1 | TCP accept/thread lifecycle, login CLI account provisioning, sessions, chat routing, per-connection bounded queues | `lancollab/server/application.py` and `FakeTransport` |
| TV2 | Shared JSON framing/codec, TLS/cert helper, file relay state/credits/ACK behavior | `lancollab/common/socket_transport.py`, schema catalog and fixtures |
| TV3 | Client CLI, input rendering, UDP discovery/RTT, contract and integration tests | `lancollab/client/application.py`, synthetic `example_messages()` |

**Hard integration dependencies:** TV1+TV3 need reviewed envelope schema before real message handlers; TV2's socket codec is needed at Checkpoint A. File transfer end-to-end depends on authenticated routing/receiver client, but chunking and protocol tests can begin immediately.

## Decisions to implement consistently

- New valid login atomically supersedes stale authenticated session; attach generation token so stale cleanup cannot log off new session.
- HELLO+AUTH 10s after TLS; three invalid attempts per connection; salted password hashes in SQLite via provisioning CLI, no network REGISTER.
- Enforce username charset; sanitize ANSI/control sequences for terminal display.
- `USER_LIST` atomic snapshot plus ordered `PRESENCE_UPDATE`; online presence includes IP+ephemeral TCP source port, offline omits those fields.
- Application sender window W=8 16-KiB chunks; receiver cumulative `FILE_ACK(next_offset)` every four chunks and final partial; window bounded at both client and server.
- Per-recipient nonblocking queues: 128 controls and 32 file frames; one fair writer, 4 control frames per 1 file frame. `FILE_END` shares FIFO with chunks. Control queue saturation => close slow client; file queue saturation => cancel transfer.
- At most one active incoming file/receiver, no server-side storage, temp-file SHA-256 checked by receiver.
- Cancel active transfers consistently using FILE_CANCEL; never rely on `ERROR` alone for an already-active transfer.
- Timeouts in [protocol spec](protocol-spec.md); peer verification and cross-platform TLS certificate instructions are part of acceptance.

## Visual references

- [Transport overview](diagrams/transport-overview.md)
- [TCP framing](diagrams/tcp-framing.md)
- [Connection, login, global/private chat](diagrams/connection-and-chat.md)
- [File offer/ACK/window, state machine](diagrams/file-transfer.md)
- [Bounded queue scheduling](diagrams/outbound-queues.md)
- [Parallel work/CP-A/CP-B/CP-C](diagrams/parallel-work-dependencies.md)

## Remaining decisions external to code

- Lecturer clarification whether TLS-only qualifies for encryption milestone.
- Confirm inconsistent assignment submission deadline and public/private repository policy.
- Record actual team reviews; do not mark networking done based on mocks or green scaffold CI.
