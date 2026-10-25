# Nonblocking queue admission and single writer — design revision 1.1

```mermaid
flowchart TB
    READER["Nonblocking per-client reader<br/>validate identity/state/window W=8"]
    RECEIVERACK["Receiver FILE_ACK(next_offset)<br/>returns credit"]
    CONTROL["Bounded control queue (128)<br/>CHAT / PING / PONG / FILE_ACK / CANCEL"]
    FILE["Bounded ordered file queue (32)<br/>FILE_CHUNK then FILE_END (same FIFO)"]
    SCHED{"Fair scheduler<br/>up to 4 controls, then 1 file"}
    WRITER["Exactly one writer<br/>complete frames, never interleaved"]
    TLS["TLS/TCP (transport HOL still possible)"]
    FAIL["File queue full → FILE_CANCEL<br/>Control queue full → close slow session"]
    READER -->|"put_nowait"| CONTROL
    READER -->|"put_nowait"| FILE
    RECEIVERACK -->|"control priority"| CONTROL
    CONTROL --> SCHED
    FILE --> SCHED
    SCHED --> WRITER --> TLS
    CONTROL -.-> FAIL
    FILE -.-> FAIL
```

- Neither `queue.put()` nor socket send/receive may block a shared registry lock or a reader responsible for incoming PONG/chat.
- Receiver sends `FILE_ACK` every four contiguous chunks or when final partial batch is complete. Sender may send at most eight unacknowledged chunks.
- On cancellation, invalidate/purge chunks and END already waiting in queue; incoming late frames for terminal transfer are ignored/rejected.
- Transfer progress shown as receiver-confirmed `next_offset`, not sender writes to local TCP.
- These are design parameters, not measured throughput or implemented behavior.
