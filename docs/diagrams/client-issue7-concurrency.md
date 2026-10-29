# TV3 concurrency, transport and file boundaries

```mermaid
flowchart LR
    INPUT["CLI input loop - TV3 done"]
    RECEIVE["Receive worker - TV3 done"]
    EVENTS["Client event dispatcher - TV3 done"]
    STATE["State and pending locks - TV3 done"]
    FILE["File sender worker - TV3 done"]
    DISK["Receiver temp file and SHA256 - TV3 done"]
    SEND["Serialized Envelope send boundary - TV3 done"]
    SOCKET["Verified TLS SocketTransport - TV2 pending"]
    SERVER["Server routing and writer queues - TV1 pending"]
    INPUT --> EVENTS
    RECEIVE --> EVENTS
    EVENTS --> STATE
    EVENTS --> FILE
    EVENTS --> DISK
    FILE --> SEND
    DISK --> SEND
    EVENTS --> SEND
    SEND --> SOCKET
    SOCKET --> RECEIVE
    SOCKET --> SERVER
```

- The file worker is the only TV3 worker that waits for FILE_ACK credit; interactive input and PING handling are separate.
- TV3's send lock prevents concurrent application calls into `transport.send`; byte framing, priority scheduling and wire-level ordering belong to TV2/TV1.
- On disconnect, transfer state and temporary downloads are cleaned. Actual concurrency guarantees require real socket and 20-client LAN evidence.