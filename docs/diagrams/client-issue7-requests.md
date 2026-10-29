# Client Issue #7 — message correlation and test boundary

This sequence describes the **implemented `CollabClient` message logic** tested with `FakeTransport.pair()`. The server-side fixture below is simulated; real TCP framing/TLS/server routing are **not** evidenced by these tests.

```mermaid
sequenceDiagram
    autonumber
    participant U as CLI caller (not yet wired)
    participant C as CollabClient (TV3)
    participant T as MessageTransport (Fake in tests)
    participant F as Simulated server endpoint
    U->>C: send_hello(client_name)
    C->>C: Generate request_id H; pending[H]=HELLO
    C->>T: send(HELLO,H)
    T->>F: Envelope HELLO
    F-->>T: Envelope HELLO_ACK(H)
    T-->>C: handle_event(HELLO_ACK,H)
    C->>C: pop H; state=WAIT_AUTH
    U->>C: login(username,password)
    Note over U,T: Real credentials require verified TLS; mock tests use synthetic values
    C->>C: Generate request_id A; remember requested username
    C->>T: send(AUTH_REQUEST,A)
    F-->>T: AUTH_RESULT(A,ok=true,username)
    T-->>C: handle_event(AUTH_RESULT,A)
    C->>C: Validate username and A; state=AUTHENTICATED
    U->>C: send_private(bob,text)
    C->>T: send(CHAT_PRIVATE,CID,to=bob,text)
    F-->>T: CHAT_ACK(CID,status=queued)
    T-->>C: handle_event(CHAT_ACK,CID)
    Note over C,F: ACK means queued at server, not delivered/read
    opt Unsolicited PING from server
      F-->>T: PING(PID,nonce)
      T-->>C: handle_event(PING,PID)
      C->>T: PONG(PID,same nonce)
    end
```

## Negative test cases

- Incorrect HELLO_ACK request ID, malformed required fields, or invalid state => `ProtocolViolation` and cleanup.
- AUTH_RESULT with a different authenticated username => reject (prevents client-side identity confusion).
- CHAT_ACK with an unknown request ID => reject rather than treating as successful delivery.
- USER_LIST or presence before authentication => reject.
- See [test_client_logic.py](../../tests/test_client_logic.py) for deterministic dummy evidence; do not claim real transport coverage.
