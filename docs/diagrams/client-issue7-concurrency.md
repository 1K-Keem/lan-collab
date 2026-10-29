# Client Issue #7 — concurrency and integration boundary

> This is a **mixed implementation / planned integration diagram**. The application-level `receive_loop`, `handle_event` and send/state locks exist in this PR. The interactive CLI thread, verified TLS bootstrap, production socket reader, and file streaming worker are **planned**, not delivered here.

```mermaid
flowchart TB
    CLI["CLI command/input loop<br/>(planned TV3)"]
    RCV["receive_loop(stop)<br/>(implemented TV3)"]
    EVENTS["handle_event(Envelope)<br/>(implemented TV3)"]
    SND["send_hello / login / chat / PONG<br/>(implemented TV3)"]
    LOCK["send lock + transport.send<br/>(implemented boundary)"]
    STATE["state lock; pending requests;<br/>presence; notices (implemented)"]
    SOCKET["SocketTransport TCP/TLS codec<br/>(TV2 planned integration)"]
    FILE["FILE sender/receiver disk workers<br/>(later Issue #8)"]
    CLI --> SND
    RCV --> EVENTS
    EVENTS --> STATE
    EVENTS -->|PONG| LOCK
    SND --> STATE
    SND --> LOCK
    FILE -.->|planned serialized sends| LOCK
    SOCKET -->|receive complete Envelope| RCV
    LOCK --> SOCKET
```

## Implementation notes / risks

1. `receive_loop` calls `transport.receive(timeout=...)` and handles timeout, EOF and protocol failure. `FakeTransport` does not block or model TCP; its test double cannot establish network concurrency guarantees.
2. `_lock` protects application state and `_send_lock` serializes `transport.send` calls; production integration must verify writer ordering at frame-byte level with TV2.
3. Never hold the application state lock during blocking network writes, file I/O, user input, or waits for FILE_ACK credit.
4. Final CLI must be responsive while the receive loop handles PING and incoming events; file workers belong to Issue #8.
5. On disconnect, transition CLOSED, clear pending and presence, and surface a notice. Need real transport tests for races, takeover and EOF.

For production architecture see [transport overview](transport-overview.md) and [file-transfer plan](file-transfer.md).
