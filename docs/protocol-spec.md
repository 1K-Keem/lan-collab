# LAN Collab application protocol — DRAFT v0.1

**Status:** DISCUSSION DRAFT. Not an approved or implemented protocol.
Before writing handlers, all three members must approve the message contract, state transitions, limits and error codes.

## Proposed TCP framing

Use a **4-byte unsigned big-endian length prefix**, followed by exactly that many UTF-8 bytes containing one JSON object. Limit frame size before reading into memory. Proposed maximum frame payload: 256 KiB.

```text
+------------------------------+------------------------------+
| Body length (4 bytes, BE)    | UTF-8 JSON body (N bytes)    |
+------------------------------+------------------------------+
```

- TCP `recv(n)` can return fewer than `n` bytes. Readers need an exact-byte loop.
- TCP can coalesce multiple writes; decode frames using the prefix, not individual `recv` calls.
- A socket writer must serialize complete frames sent by multiple producers.

## Proposed JSON envelope

```json
{
  "version": 1,
  "type": "CHAT_PRIVATE",
  "request_id": "example-001",
  "payload": {
    "to": "bob",
    "text": "Hello"
  }
}
```

`request_id` associates a request with its acknowledgment/error. The server sets `from` using the authenticated session instead of trusting the client.

## Message families to agree on

| Category | Suggested types |
| --- | --- |
| Handshake | `HELLO`, `HELLO_ACK` |
| Auth and presence | `AUTH_REQUEST`, `AUTH_RESULT`, `USER_LIST`, `PRESENCE_UPDATE`, `LOGOUT` |
| Messaging | `CHAT_GLOBAL`, `CHAT_PRIVATE`, `CHAT_ACK` |
| File | `FILE_OFFER`, `FILE_ACCEPT`, `FILE_REJECT`, `FILE_CHUNK`, `FILE_END`, `FILE_RESULT`, `FILE_CANCEL` |
| Control/errors | `PING`, `PONG`, `ERROR` |
| UDP discovery | `DISCOVER`, `DISCOVER_REPLY` |

## File transfer proposed behavior

Sender offers `transfer_id`, destination username, filename, byte size and SHA-256 hash. Receiver accepts or rejects. Sender then streams fixed-size chunks. Receiver writes a temporary file, checks exact size and SHA-256, and acknowledges success/failure before atomic rename. Suggested raw chunk size: 64 KiB, base64-encoded inside JSON. Limit concurrent transfers/queue sizes.

**TCP already guarantees in-order reliable bytes on a live connection**, but this does not guarantee that an interrupted application-level file transfer completed. Final acknowledgment requires receiver-side validation.

## Security decisions

- Establish TLS before sending credentials or messages.
- Use server-verified authentication after TLS handshake.
- Store password hashes with salts (never plaintext).
- Validate session identity, recipient permissions, frame sizes, filenames, and transfer IDs.
- UDP discovery is not trusted authentication.

## MUST finalize before development

- [ ] Required fields and exact payload schema for every message.
- [ ] Allowed state transitions, sequence diagrams and error codes.
- [ ] Numeric limits and timeouts.
- [ ] Consistent semantics for ACK (server accepted/forwarded versus recipient stored/read).
- [ ] Sender/receiver behavior on cancellation and disconnect.
- [ ] Versioning and protocol compatibility strategy.

## Suggested test vectors to add

- Two frames coalesced into one TCP read.
- One frame fragmented across multiple TCP reads.
- Invalid length/version/JSON/type.
- Empty file, binary file, interrupted transfer and mismatched hash.
