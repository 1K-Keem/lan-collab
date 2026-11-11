# UDP discovery and network-quality probes

```mermaid
sequenceDiagram
    autonumber
    participant C as UDP Client
    participant U as Server UDP responder
    participant T as Verified TCP/TLS connection
    C->>U: DISCOVER with instance_id
    U-->>C: DISCOVER_REPLY with echoed instance_id
    C->>T: Connect TCP host and verify TLS server name
    loop Explicit network probes
        C->>U: HEARTBEAT with instance_id and seq
        alt Probe delivered
            U-->>C: HEARTBEAT_ACK with same seq
            Note over C: Compute RTT
        else Timeout
            Note over C: Count packet loss
        end
    end
```

- Client binds a UDP ephemeral port; server UDP port defaults to 40001.
- UDP is not authenticated and does **not** change TCP session presence or auth state. Use `--host` if broadcast is blocked.
- Both client and server participate; record two-host discovery and fallback evidence as described in [acceptance](../acceptance.md).
