# Parallel work and integration checkpoints

Three team members can implement independently using a shared message contract and in-memory `FakeTransport`. **Mocks alone do not satisfy any real networking milestone.**

```mermaid
flowchart TB
    P["Protocol v1.0 baseline + typed contracts<br/>Owner has frozen design; team review pending"]
    S["TV1: TCP server + sessions + routing<br/>Dummy MessageTransport first"]
    CODEC["TV2: real framing + TLS + file chunking"]
    C["TV3: client CLI + UDP + tests<br/>Dummy MessageTransport first"]
    A["CP-A: real TCP framing + HELLO / HELLO_ACK"]
    B["CP-B: 3-client authentication + chat + presence"]
    D["CP-C: file transfer + TLS + resilience + 20 clients"]
    FINAL["Report, reproducible README, oral walkthrough"]

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

**Gates:** [CP-A — Issue #16](https://github.com/1K-Keem/lan-collab/issues/16), [CP-B — Issue #17](https://github.com/1K-Keem/lan-collab/issues/17), [CP-C — Issue #18](https://github.com/1K-Keem/lan-collab/issues/18). Exact acceptance criteria are in [checkpoints.md](../checkpoints.md).
