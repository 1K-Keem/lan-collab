# TV3 file sender and receiver client-side pipeline — implemented with FakeTransport tests

```mermaid
sequenceDiagram
    autonumber
    participant A as Sender client TV3
    participant R as Relay server TV1 pending
    participant B as Receiver client TV3
    A->>A: Hash file before offer
    A->>R: FILE_OFFER with hash and size
    R->>B: FILE_OFFER
    Note over B: User sees consent prompt
    B->>R: FILE_ACCEPT
    R->>A: FILE_ACCEPT
    loop Up to eight unacknowledged chunks
        A->>R: FILE_CHUNK ordered
        R->>B: FILE_CHUNK
        B->>B: Write contiguous chunk to temp
        opt Four chunks or final partial
            B->>R: FILE_ACK with next_offset
            R->>A: FILE_ACK
            Note over A: Receiver-confirmed progress only
        end
    end
    A->>R: FILE_END after all bytes sent
    R->>B: FILE_END
    B->>B: Verify count size and SHA256
    B->>B: Atomically commit without overwrite
    B->>R: FILE_RESULT with verification status
    R->>A: FILE_RESULT
```

- Empty file sends zero chunks; receiver sends ACK offset 0 after END and then FILE_RESULT.
- Chat/PONG workers must not wait for ACK credit. END is queued behind chunks without waiting for all ACKs.
- Only `FILE_RESULT(ok=true)` is success. 100% acknowledged bytes does not prove file integrity.
- TV2 owns reusable socket-independent transfer validation and TV1 the relay; this diagram is **client logic tested with simulated relay**, not real end-to-end LAN evidence.