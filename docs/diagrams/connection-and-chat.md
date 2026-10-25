# Connection, authentication and messaging — proposed sequence

**Design artifact** for Issue #2 and CP-A/CP-B. Must be updated to match the implementation.

```mermaid
sequenceDiagram
    autonumber
    participant A as Client Alice
    participant S as Central Server
    participant B as Client Bob
    A->>S: TCP connect to host:40000
    Note over A,S: TLS handshake with cert validation (worker timeout)
    A->>S: HELLO (length-prefixed JSON)
    S-->>A: HELLO_ACK
    A->>S: AUTH_REQUEST(username,password)
    alt Authentication succeeds
        S-->>A: AUTH_RESULT(ok=true)
        S-->>A: USER_LIST(online users)
        S-->>B: PRESENCE_UPDATE(Alice online)
        A->>S: CHAT_PRIVATE(to=Bob,text)
        alt Bob is online
            S-->>B: CHAT_PRIVATE(from=Alice,text)
            S-->>A: CHAT_ACK(status=queued)
            Note over A,B: ACK only means server queued for routing
        else Bob unavailable
            S-->>A: ERROR(USER_OFFLINE)
        end
    else Invalid credentials
        S-->>A: AUTH_RESULT(ok=false,AUTH_FAILED)
    end
    opt Client logs out or TCP disconnects
        S-->>B: PRESENCE_UPDATE(Alice offline)
    end
```

**TCP data framing:** each application frame is 4-byte BE payload length plus JSON bytes. TLS handles encryption; there is no second JSON-over-HTTP layer.

**UDP separately:** DISCOVER/PRESENCE_BEACON broadcasts, server DISCOVER_REPLY unicast, and unauthenticated HEARTBEAT telemetry do not grant auth or authoritative online state. Manual IP fallback must work when UDP is blocked.
