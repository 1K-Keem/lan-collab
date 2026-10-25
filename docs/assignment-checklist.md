# Assignment rubric and acceptance checklist

Source: *Assignment 1 — Distributed Client-Server Collaboration Application*, Network Application Development (uploaded project brief).

> Instructor clarification needed: page 1 says 6 weeks, page 5 says 8 weeks; the written **suggested** deadline says “Sunday, November 17, 2026” although 2026-11-17 is a Tuesday; submission allows a private repository link but academic integrity instructions say to push to a public repository. The team is targeting a one-week implementation window. Confirm the official deadline and visibility.

| # | Milestone | Owner(s) | Evidence required |
| --- | --- | --- | --- |
| 1 | Protocol & setup | All / TV2 lead | Wire format, message types, state diagrams, protocol spec |
| 2 | TCP handshake | TV1 + TV3 | Connect/ack captured in logs |
| 3 | Concurrency | TV1 | Multiple simultaneous connections and disconnects |
| 4 | Authentication & state | TV1 | Login, IP/active-port tracking, user list |
| 5 | Text routing | TV1 + TV3 | Global and private chat tests |
| 6 | Reliable file transfer | TV2 | Chunking, reassembly and identical SHA-256 |
| 7 | Network resilience | TV1 + TV3 | Timeout/broken pipe/disconnect evidence |
| 8 | Security layer | TV2 | Encrypted TCP traffic, certificate verification |
| 9 | UI/CLI | TV3 | Message, online list, network status, file progress |
| 10 | Testing & quality | All / TV3 lead | Local multi-terminal and LAN/concurrency tests |

## Submission deliverables

- [ ] Client and server source scripts, shared protocol code.
- [ ] README with strict install/start/connect instructions.
- [ ] Technical report PDF with high-level architecture diagram.
- [ ] Custom packet/message structures and protocol specification.
- [ ] Sequence diagrams for message and file transfer lifecycle.
- [ ] Approximately one-page Challenges & Solutions reflection.
- [ ] Actual test results and representative screenshots/logs.
- [ ] Commit history showing individual contributions.

## Scope guardrails (one-week schedule)

**Must-have:** authenticated chat, online users, file transfer with integrity, 10–20 concurrent clients, TLS, UDP discovery/heartbeat, CLI and tests.

**Out of scope unless all rubric items work:** GUI, multiple chat rooms, voice/video, direct P2P, resume after broken connection, multi-server federation, message history.

## Acceptance scenarios

1. Three separate clients connect/login and receive consistent online user lists.
2. Global message reaches online users; private message reaches only the intended recipient.
3. Transfer a binary file, verify identical SHA-256 after receiving.
4. Kill a client mid-transfer and confirm server stays alive, cleans up session and can accept reconnect.
5. Run the 20-client chat/file workload and explicit latency/error/resource acceptance targets in checkpoints.md, including slow-consumer and reconnect cases.
6. Run clients across two devices on the same LAN and verify TCP/TLS and discovery or manual IP fallback.
7. Capture TLS traffic and verify no plaintext application payload is exposed.
