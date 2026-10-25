# Parallel development and checkpoint gates — v1.1

```mermaid
flowchart TB
    P["v1.1 contract + schema catalog<br/>Owner revision, TV1/TV2/TV3 review pending"]
    S["TV1: TCP server, sessions, routing<br/>FakeTransport first"]
    CODEC["TV2: framing, TLS, file ACK/window"]
    C["TV3: client, CLI, UDP, tests<br/>FakeTransport first"]
    A["CP-A: real TCP framing + HELLO"]
    B["CP-B: 3-client login/chat/presence"]
    D["CP-C: file ACK/window, TLS, 20-client tests"]
    FINAL["Report and oral walkthrough"]
    P --> S
    P --> CODEC
    P --> C
    S --> A
    CODEC --> A
    C --> A
    A --> B
    B --> D
    CODEC --> D
    D --> FINAL
```

The team reviews v1.1 contract in [Issue #2](https://github.com/1K-Keem/lan-collab/issues/2). [CP-A #16](https://github.com/1K-Keem/lan-collab/issues/16), [CP-B #17](https://github.com/1K-Keem/lan-collab/issues/17), [CP-C #18](https://github.com/1K-Keem/lan-collab/issues/18) require **actual socket-based evidence**, not dummy-only CI.
