# TV2 deliverables: how TV1 and TV3 use them

Everything here is implemented against protocol revision 1.2 and covered by real-socket tests
(`tests/test_codec.py`, `test_socket_transport.py`, `test_tls.py`, `test_transfer_core.py`,
`test_tv2_integration.py`). Run them with `python -m unittest discover -s tests`.

| Module | Purpose |
| --- | --- |
| `common/codec.py` | `JsonFrameCodec(role)`: 4-byte length + strict JSON, schema/direction/conditional validation, `RequestIdTracker` |
| `common/socket_transport.py` | `SocketTransport`: exact reads, absolute deadlines, one serialized writer, `connect_verified` / `accept_verified` |
| `common/tls.py`, `common/devcerts.py` | verified TLS contexts, bounded handshakes, CA bundling/resolution, certificate generator ([tls-setup.md](tls-setup.md)) |
| `common/transfer_core.py` | `TransferRegistry` (server), `OutgoingTransfer` (sender), `IncomingTransfer` + `DiskSink` (receiver), `AckLedger` |
| `common/filenames.py` | receiver-side `sanitize_filename` |

## Error model (read this first)

```python
try:
    msg = transport.receive(timeout=...)
except MessageError as exc:  # valid envelope + request_id, bad content; stream still in sync
    transport.send(exc.to_error_envelope())  # ERROR copying the same request_id
except FrameError:  # unparsable / no trustworthy request_id; transport already closed
    ...  # just clean the session up, do NOT send ERROR
except TimeoutError:  # nothing arrived in `timeout`; nothing consumed (or, for a
    ...  # half-received frame / stalled write, transport was closed)
except ConnectionError:  # EOF, reset or closed => session cleanup
    ...
```

`SocketTransport.send` validates the outgoing message against the same catalog and raises
`OutboundMessageError` (a local bug, nothing written). Sends from any thread are serialized
whole-frame; a reader thread and a writer thread can share one transport.

## For TV1 (server)

* One `SocketTransport.accept_verified(ctx, raw)` per accepted socket, **inside a worker**.
* Keep a `RequestIdTracker` per session; call `register()` for every client request except
  `PONG`. `MessageError` -> reply `ERROR`; `FrameError` (history exhausted) -> close.
* File transfers are `TransferRegistry`. On authentication success call
  `registry.open_session(identity)`; on any close/takeover call
  `registry.close_session(session_id, "DISCONNECTED" | "SESSION_REPLACED")`.
* `FILE_OFFER`: resolve `payload["to"]` to a `SessionIdentity`, then `registry.offer(sender,
  receiver, msg)`. Other `FILE_*`: `registry.handle(session, msg)`.
* Every call returns an `Outcome`. After releasing your locks: send `outcome.error.reply(
  msg.request_id)` if set, enqueue each `Forward` (non-blocking) on `lane` `"control"` or
  `"file"` for `fwd.recipient`, and drop queued `FILE_CHUNK`/`FILE_END` frames whose
  `transfer_id` is in `outcome.purge`.
* If a file-queue put fails: `registry.fail_queue_full(transfer_id)`. If a control queue is
  full: close that session and call `close_session`.
* Writer reports completed writes: `registry.mark_written(transfer_id, "FILE_OFFER")` after the
  offer was written to the receiver, `"FILE_ACCEPT"` after it was written to the sender,
  `"FILE_END"` after it was written to the receiver. These start the 60 s / 10 s / 30 s
  deadlines; the 15 s stall watchdog runs from admission. Call `registry.expire()` about once a
  second and handle the returned `Outcome` the same way.

## For TV3 (client)

* `ca_file = resolve_ca_file(args.ca_file)` (already parsed by `python -m lancollab.client`:
  `--ca-file`, then `$LANCOLLAB_CA_FILE`, then the bundled `lancollab/client/ca.pem`), then
  `SocketTransport.connect_verified(host, port, ca_file)`; send `HELLO`, wait for `HELLO_ACK`.
  Show a certificate failure (`ssl.SSLCertVerificationError`) as "server identity could not be
  verified" and never offer to continue anyway.
* Sender: `out = OutgoingTransfer(tid, size)`; reader thread calls `out.on_accept()`,
  `out.on_ack(n)`, `out.on_result(ok, reason)`, `out.on_reject()`, `out.on_cancel(reason)`.
  The **sender worker** loops `out.wait_for_credit(); payload = out.next_chunk_payload(data)`,
  sends `FILE_CHUNK`, then `FILE_END` with `out.end_payload()` immediately after the last byte
  (no ACK drain). Progress = `out.acked_offset`; success only when `out.state is DONE`.
  `hash_file(path)` gives `(size, sha256)` for the offer.
* Receiver: `IncomingTransfer(tid, name, size, sha256, DiskSink(downloads_dir, name))`.
  `on_chunk(payload)` returns ACK offsets to send immediately; `on_end(total)` returns
  `EndResult(acks, ok, reason, saved_to)`: send the ACKs, then `FILE_RESULT`. On `FILE_CANCEL`
  or session replacement call `cancel()` (removes the partial file; late frames are ignored).
* **On disconnect** call `cancel()` on every active `IncomingTransfer` and `on_cancel(...)` on
  every `OutgoingTransfer`; otherwise the receiver's `.part` file stays open on disk.
* Display text only after your own control-character/ANSI filtering (not part of TV2).

## Decisions TV2 made where the spec was silent (please review in Issue #2)

1. Plain `str` fields: `client_name`/`server_name` 1-128 bytes, `password` 1-1024, `message`
   1-1024, `nonce` 1-64 printable ASCII, other strings 1-4096. `reason` fields 1-256 bytes.
2. `transfer_id` must be a canonical lowercase UUID string. `name` must be a plain basename
   (no `/`, `\`, control characters, `.` or `..`); Windows-reserved names are handled by the
   receiver's `sanitize_filename`, as the spec requires.
3. `FILE_OFFER.size` accepts any integer up to 2**53 on the wire so the core can answer
   `FILE_TOO_LARGE` (limit 50 MiB); offsets/sequence numbers are capped at 50 MiB.
4. `data_b64` must be canonical padded base64 of 1-16384 bytes.
5. Protocol violations by a transfer participant (bad sequence/offset, window overrun, bad ACK,
   premature END/RESULT) reply `ERROR` to the offender **and** abort the transfer with
   `FILE_CANCEL(reason=<code>)` to both connected participants. A message from the wrong role
   (e.g. receiver sending `FILE_CHUNK`) gets `ERROR(INVALID_STATE)` without aborting; a
   non-participant gets `TRANSFER_NOT_FOUND` and nothing changes.
6. A duplicate (equal-offset) `FILE_ACK` is silently ignored; the first ACK at `size` is always
   forwarded (needed for empty files, whose ACK 0 must precede a successful `FILE_RESULT`).
7. Duplicate client request ids get `ERROR(INVALID_MESSAGE)` with the repeated id.

## Not part of TV2 (still open)

TCP accept loop, authentication/SQLite, presence, chat routing and bounded writer queues (TV1);
CLI, UDP discovery/RTT, control-character sanitation, the real client file worker (TV3); the
two-host LAN run, Wireshark capture, 20-client load measurements and the lecturer's answer
about TLS-only encryption (team, CP-C). `tests/test_tv2_integration.py` uses a throw-away relay
to show the TV2 pieces work together over verified TLS; it is not a substitute for those.
