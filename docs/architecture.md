# Architecture

LAN Collab uses one central server and one persistent TLS connection per client.
The TUI and plain CLI share client state, commands and file handling. See the
[transport diagram](diagrams/transport-overview.md) and [user guide](client-guide.md).

## Components

| Component | Responsibility |
| --- | --- |
| `client/__main__.py`, `auth_tui.py`, `cli.py`, `tui.py` | Connection setup, authentication forms and terminal interfaces |
| `client/application.py` | Authenticated state, requests/responses, presence, chat and file-event dispatch |
| `client/transfers.py` | File consent, sender worker, receiver writes, cumulative ACKs and verified save |
| `client/history.py`, `token_store.py` | Local conversation history and remembered login |
| `client/discovery.py`, `server/discovery.py` | UDP discovery and explicit network-quality probes |
| `common/codec.py`, `message_schema.py`, `message_schema.json` | Length-prefixed JSON and strict field/type/direction validation |
| `common/socket_transport.py`, `tls.py` | Serialized frame writes, bounded reads and verified TLS |
| `common/transfer_core.py` | Session-bound transfer authorization, window credit and deadlines; independently tested transfer primitives |
| `server/connections.py`, `outbound.py` | Concurrent connection lifecycle, liveness and bounded writer queues |
| `server/application.py` | Account approval, session registry, takeover, presence and routing |
| `server/accounts.py`, `tokens.py`, `queue.py` | Password/token hashes and bounded offline private-message storage |

## Data paths

TCP/TLS carries login, presence, chat, file chunks and transfer control. Frames have
a four-byte big-endian length followed by UTF-8 JSON. Each connection has one writer;
file END stays behind its chunks, while control traffic has bounded priority.

UDP discovery finds a candidate endpoint. The client verifies that server through
TLS before sending credentials. UDP heartbeat reports network quality; TCP
PING/PONG determines session liveness. See [UDP flow](diagrams/client-udp-probes.md).

The server relays files without persisting their contents. The receiver writes to
a temporary file, checks size/order/SHA-256 and commits a non-overwriting filename.
ACK progress confirms received bytes; only the final success result confirms a
verified file. See [file lifecycle](diagrams/file-transfer.md).

Each login has a distinct session ID. Takeover cancels the old session's transfers;
old events and cleanup cannot target a replacement login. The server uses bounded
queues and transfer windows to limit memory. Full state/limit rules live in the
[protocol specification](protocol-spec.md), with the thread lifecycle in
[server connections](server-connections.md).

## Storage and trust

- Server SQLite: accounts, salted password hashes, token hashes and pending offline
  private messages (up to 100 per recipient, expiring after 24 hours).
- Client SQLite: saved chat history, scoped by server identity and user account.
- Client token store: remembered login, separate from chat history.
- Receiver disk: verified downloads and temporary files during an active transfer.
- Server key: private; clients receive only its public certificate through the
  agreed trusted distribution channel. See [TLS setup](tls-setup.md).

TLS encrypts each client-server connection. The server can read messages and relay
file contents; this is not E2EE or direct peer-to-peer networking. Transfers do not
resume after disconnect. Offline private-message delivery is at most once and is
not a substitute for client history.

## Verification

[Acceptance instructions](acceptance.md) separate automated unit/loopback evidence
from the two-host, packet-capture and terminal checks needed for submission.
The [assignment checklist](assignment-checklist.md) maps those checks to the rubric.
