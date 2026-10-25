# Transport and architecture overview — design revision 1.2

```mermaid
flowchart LR
    subgraph LAN["LAN / reachable private network"]
        A["Alice Client"]
        B["Bob Client"]
        C["Charlie Client"]
        S["Central Server"]
        A <-->|"TCP/TLS :40000"| S
        B <-->|"TCP/TLS :40000"| S
        C <-->|"TCP/TLS :40000"| S
        A -.->|"UDP DISCOVER / HEARTBEAT"| S
        B -.->|"UDP DISCOVER / HEARTBEAT"| S
        C -.->|"UDP DISCOVER / HEARTBEAT"| S
        S -.->|"DISCOVER_REPLY / HEARTBEAT_ACK"| A
        S -.->|"DISCOVER_REPLY / HEARTBEAT_ACK"| B
        S -.->|"DISCOVER_REPLY / HEARTBEAT_ACK"| C
    end
    S --- AUTH["Authentication + Sessions"]
    S --- MSG["Chat + Presence routing"]
    S --- RELAY["Bounded, ACK-controlled file relay"]
```

- Client UDP sockets bind **ephemeral source ports** (server listens on UDP 40001); discovery can use broadcast, heartbeat unicast.
- UDP replies help measure RTT/loss. They have **no security or session-state authority**.
- TCP/TLS carries authenticated presence, chat and file data; verified TLS server identity is independent of changing DHCP address.
- See [protocol spec](../protocol-spec.md), [file transfer](file-transfer.md), [queue scheduler](outbound-queues.md).
