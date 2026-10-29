# TV3 UDP discovery and liveness probes — implemented client, pending server

```mermaid
sequenceDiagram
    autonumber
    participant C as TV3 UDP Client
    participant U as LAN UDP responder - TV1 pending
    participant T as TV2 TCP TLS connection - pending
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
- Only client code is implemented in this PR; real LAN discovery requires TV1's responder and network-level testing.