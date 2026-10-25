# Architecture — working design draft

**Status:** PROPOSED; team review required before implementation.

## Goal

A self-hosted, single-server system for users on the same LAN (or network with IP reachability). A central server handles authentication, presence, messages and file relay. No direct peer-to-peer transport in the MVP.

```text
Client A --- TCP/TLS ---+
Client B --- TCP/TLS ---+--- Collaboration Server
Client C --- TCP/TLS ---+      | session registry
                               | message routing
Clients ---- UDP discovery --- | file relay
```

## Module responsibilities

- `common/`: wire envelope, message validation, frame encoder/decoder and shared constants.
- `server/`: accept loop, authenticated session registry, global/private routing, file relay and cleanup.
- `client/`: socket connection, reader/writer lifecycle, CLI, discovery, progress.
- `tests/`: framing edge cases, routing, file integrity, concurrent clients and disconnect scenarios.

## Proposed runtime

- TCP for control messages and reliable file chunks.
- TLS over TCP using Python `ssl`. Client must verify server identity/certificate; never disable certificate checks in the final implementation.
- UDP broadcast for server discovery on LAN, with manual host/IP fallback.
- Application heartbeat / timeout handling. Do not authenticate users based solely on spoofable UDP messages.
- Thread-per-client or asynchronous I/O are allowed by the assignment. Initial decision: threaded server with a bounded outbound queue per connection and serialized socket writes.

## Session lifecycle

```text
TCP_CONNECTED -> TLS_ESTABLISHED -> HELLO_DONE -> AUTHENTICATED -> ACTIVE
      |                 |                 |              |           |
      +-----------------+-----------------+--------------+-----------+
                                 |
                        DISCONNECTED / CLOSED
                                 |
                               CLEANUP
```

## Key constraints

- TCP is a byte stream; implement explicit application framing and exact-length reads.
- Validate lengths/types before allocation/processing.
- Never hold a global session lock across potentially blocking socket I/O.
- Treat client-provided sender identifiers as untrusted; bind identity to authenticated session.
- Use file transfer IDs, temporary destination paths, and a cryptographic hash for integrity.
- Clearly distinguish server-mediated encryption (TLS) from end-to-end encryption.

## Team decisions to confirm

- [ ] Thread-per-client + outbound writer queue versus `selectors`.
- [ ] Authentication policy (registration or pre-provisioned accounts).
- [ ] Duplicate login behavior and online/offline semantics.
- [ ] Maximum message/file size; filename/path restrictions.
- [ ] Whether UDP heartbeat is separate from TCP application heartbeat.
- [ ] Manual server certificate distribution for LAN demo.

Architecture and sequence diagrams should be updated once actual behavior is implemented.
