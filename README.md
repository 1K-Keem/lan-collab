# LAN Collab

A self-hosted LAN collaboration application for a **Network Application Development** course assignment.

> **Status:** Planning / repository scaffold. Application features are **not yet implemented**.

## Project scope

Build a distributed client-server collaboration application using **raw TCP/UDP sockets**, with:

- User authentication and live peer presence
- Global chat and private messaging
- Reliable file transfer with chunking and integrity verification
- Concurrent clients (target: 20 active connections)
- Network resilience (timeouts, disconnects and cleanup)
- Encrypted TCP connections
- Clear CLI, file-transfer progress and online user list

No Flask, Django, HTTP frameworks or WebSockets. See [Assignment checklist](docs/assignment-checklist.md).

## Proposed technology

Python 3.11+, `socket`, `threading`, `ssl`, `sqlite3`, `json`, `struct`, `hashlib`; optional `pytest` for tests. Prefer Python standard-library modules for networking.

## Team ownership

| Owner | Responsibility |
| --- | --- |
| TV1 | TCP server, concurrency, authentication, session state, chat routing |
| TV2 | Shared protocol, framing, file transfer, SHA-256 and TLS |
| TV3 | TCP client, CLI, UDP discovery, automated integration testing |

**TV1/TV2/TV3 are role placeholders.** Assign GitHub usernames after team members are added as collaborators. Everyone reviews interface changes and contributes to integration/testing/reporting.

## Project layout

```text
client/            # Client networking, CLI, discovery, transfer UI
server/            # TCP server, sessions, routing, file relay
common/            # Shared wire protocol and codecs
tests/             # Unit and integration tests
docs/              # Architecture, protocol, report materials
scripts/           # Multi-client simulation and demo helpers
.github/           # CI and PR template
```

## Getting started

1. Clone: `git clone https://github.com/1K-Keem/lan-collab.git`
2. Enter: `cd lan-collab`
3. Use Python 3.11+ and a virtual environment:
   - Linux/macOS: `python3 -m venv .venv && source .venv/bin/activate`
   - Windows PowerShell: `py -3 -m venv .venv; .venv\Scripts\Activate.ps1`
4. For developer tools: `python -m pip install -r requirements-dev.txt`
5. Run scaffold checks: `python -m compileall -q client server common tests`
6. Run tests after implementation: `python -m unittest discover -s tests -p "test_*.py"`

**Server/client run commands will be documented after the team implements them.** The repository scaffold alone is not a working application.

## Development workflow

- `main` should remain reviewable; create branches such as `feature/server-core`, `feature/protocol`, `feature/client-cli`, `test/integration`.
- Open an Issue before substantial work; link your PR with `Closes #<issue>`.
- Agree on message schemas in `docs/protocol-spec.md` before implementing handlers. Any incompatible change requires joint review.
- Keep commits attributable to their actual authors; each member must understand and explain their own implementation.

## Documentation

- [Architecture and decisions](docs/architecture.md)
- [Protocol draft (requires team approval)](docs/protocol-spec.md)
- [Assignment milestones and validation](docs/assignment-checklist.md)
- [Team contribution workflow](CONTRIBUTING.md)

## Submission

Include client/server source, clear reproduction commands, Technical Explanation Report (PDF), sequence diagrams, protocol specification and network challenge/solution reflection. Confirm the official submission deadline and repository visibility with the instructor: the assignment document contains inconsistent statements.

## Academic integrity

The assignment permits AI assistance for **syntax reference and debugging**, but requires students to explain their implementation fully. This repository starts with organizational scaffolding only. Team members should write and understand the graded networking implementation themselves.
