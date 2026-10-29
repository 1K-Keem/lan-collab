# TV3 HELLO, AUTH and chat correlation — implemented state / planned socket wiring

```mermaid
sequenceDiagram
    autonumber
    participant U as Terminal User
    participant C as TV3 Client
    participant T as MessageTransport
    participant S as Server or Fake
    Note over T,S: In tests transport and server are simulated
    U->>C: connect and login
    C->>T: HELLO with request H
    T->>S: HELLO
    S-->>T: HELLO_ACK with H
    T-->>C: HELLO_ACK
    C->>T: AUTH_REQUEST with request A
    T->>S: AUTH_REQUEST
    S-->>T: AUTH_RESULT with A
    T-->>C: AUTH_RESULT
    Note over C: Validate exact requested username
    U->>C: private message to bob
    C->>T: CHAT_PRIVATE with request C1
    T->>S: CHAT_PRIVATE
    S-->>T: CHAT_ACK with C1
    T-->>C: CHAT_ACK
    Note over C: Queued is not delivered or read
    opt Server checks liveness
        S-->>T: PING with request P
        T-->>C: PING
        C->>T: PONG with request P
    end
```

- Request IDs are unique per session; direct responses must match the pending request ID. File events are correlated by transfer UUID; server ERROR replies can still correlate to the original file request ID.
- Source: `lancollab/client/application.py`; tests: `tests/test_client_logic.py`, `tests/test_client_cli.py`.
- The diagram shows application messages, **not** evidence of functioning TV1/TV2 real networking.