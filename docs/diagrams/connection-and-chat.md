# TLS, authentication, global/private chat and liveness — design revision 1.1

```mermaid
sequenceDiagram
    autonumber
    participant A as Alice
    participant S as Server
    participant B as Bob
    participant C as Charlie
    A->>S: TCP connect
    Note over A,S: TLS identity verified (5s timeout, worker)
    A->>S: HELLO
    S-->>A: HELLO_ACK (same request_id)
    A->>S: AUTH_REQUEST(username,password)
    Note over A,S: HELLO + AUTH within 10s of TLS
    alt Credentials correct
        Note over S: Atomically register session + snapshot + event ordering
        S-->>A: AUTH_RESULT(ok=true)
        S-->>A: USER_LIST(username,ip,ephemeral port)
        S-->>B: PRESENCE_UPDATE(Alice online,ip,port)
        S-->>C: PRESENCE_UPDATE(Alice online,ip,port)
        A->>S: CHAT_GLOBAL(text)
        S-->>B: CHAT_GLOBAL(from=Alice,text)
        S-->>C: CHAT_GLOBAL(from=Alice,text)
        S-->>A: CHAT_ACK(status=queued)
        A->>S: CHAT_PRIVATE(to=Bob,text)
        S-->>B: CHAT_PRIVATE(from=Alice,text)
        S-->>A: CHAT_ACK(status=queued)
        opt Periodic liveness
            S->>A: PING(nonce)
            A->>S: PONG(same nonce)
        end
    else Invalid credentials
        S-->>A: AUTH_RESULT(ok=false,error_code=AUTH_FAILED)
        Note over S: After 3 failures or auth deadline, close connection
    end
    opt Alice logs out or times out
        A->>S: LOGOUT or TCP closes
        S-->>B: PRESENCE_UPDATE(Alice offline)
        S-->>C: PRESENCE_UPDATE(Alice offline)
    end
```

Server sets authenticated `from` on routed messages. `CHAT_ACK` means server queue admission, **not** Bob/Charlie reading the message. For invalid recipient, server sends `ERROR` with the rejected request's exact `request_id`. Terminal CLI must escape malicious ANSI/control characters.
