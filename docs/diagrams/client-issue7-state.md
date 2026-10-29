# TV3 client state machine — message logic (implemented, mock tested)

```mermaid
stateDiagram-v2
    [*] --> READY
    READY --> WAIT_HELLO_ACK: send HELLO
    WAIT_HELLO_ACK --> WAIT_AUTH: matching HELLO_ACK
    WAIT_AUTH --> WAIT_AUTH_RESULT: send AUTH_REQUEST
    WAIT_AUTH_RESULT --> AUTHENTICATED: matching AUTH_RESULT success
    WAIT_AUTH_RESULT --> WAIT_AUTH: matching AUTH_RESULT failure
    AUTHENTICATED --> AUTHENTICATED: chat presence file and PING
    READY --> CLOSED: transport failure
    WAIT_HELLO_ACK --> CLOSED: invalid handshake or EOF
    WAIT_AUTH --> CLOSED: logout or EOF
    WAIT_AUTH_RESULT --> CLOSED: invalid event or EOF
    AUTHENTICATED --> CLOSED: logout takeover or EOF
    CLOSED --> [*]
```

- Code: `lancollab/client/application.py` and CLI in `lancollab/client/cli.py`.
- Incoming `AUTH_RESULT.username` must match the pending login username; untrusted control characters in chat are escaped.
- The `auth_allowed` testing flag is not proof of TLS. `__main__.py` calls a TV2-provided **verified** transport factory; until TV2 implements it, no real login is available.
- Tests with FakeTransport are **not** authenticated real TCP integration evidence.