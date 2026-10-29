# Client Issue #7 — application state machine (implemented mock logic)

> Scope: transport-independent `CollabClient` on this feature branch. `READY` means an injected transport is supplied, **not** that TCP/TLS has been verified. Actual TLS bootstrap and CLI are integration TODOs. On-wire version remains 1.

```mermaid
stateDiagram-v2
    [*] --> READY: Construct CollabClient(transport)
    READY --> WAIT_HELLO_ACK: send_hello / remember request_id
    WAIT_HELLO_ACK --> WAIT_AUTH: valid HELLO_ACK / same request_id
    WAIT_HELLO_ACK --> CLOSED: HELLO ERROR, invalid response, send failure
    WAIT_AUTH --> WAIT_AUTH_RESULT: login / AUTH_REQUEST [verified TLS required in production]
    WAIT_AUTH_RESULT --> AUTHENTICATED: AUTH_RESULT ok=true / username matches
    WAIT_AUTH_RESULT --> WAIT_AUTH: AUTH_RESULT ok=false / clear requested username
    WAIT_AUTH_RESULT --> WAIT_AUTH: matched ERROR / clear requested username
    AUTHENTICATED --> AUTHENTICATED: CHAT, USER_LIST, PRESENCE, PING/PONG
    WAIT_AUTH --> CLOSED: LOGOUT, EOF, protocol error
    AUTHENTICATED --> CLOSED: LOGOUT, EOF, protocol error
    WAIT_AUTH_RESULT --> CLOSED: EOF, protocol error
    CLOSED --> [*]
```

## State invariants

- `HELLO` only in `READY`; `AUTH_REQUEST` only after matching `HELLO_ACK`; chat/presence only after matching `AUTH_RESULT(ok=true)`.
- A successful `AUTH_RESULT.username` must equal the username requested by this client; mismatches reject the event and close the session.
- `request_id` is generated per outgoing request, held in `_pending`, and direct replies consume the matching pending entry. PONG is a response and echoes the received request ID.
- `close()` is idempotent and clears username, requested identity, presence, and pending requests.
- Status above is application-level state; it does not imply real network acceptance or secure authentication.

Implementation: [client application](../../lancollab/client/application.py). Related existing design: [connection and chat](connection-and-chat.md).
