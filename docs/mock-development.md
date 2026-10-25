# Parallel development with contract v1.1 and in-memory dummies

Only interfaces, declarative field catalog and synthetic examples are provided here. Real TCP/TLS, UDP, chat, authentication, flow-control and file-transfer implementations are **not** provided or tested.

- `lancollab/common/contracts.py`: `Envelope`, `MessageTransport`, `ProtocolCodec`, limits.
- `lancollab/common/message_schema.py`: declarative message fields, directions and initial limits; **not** a runtime validator.
- `lancollab/common/fake_transport.py`: in-memory peer queues, **not** TCP/UDP/TLS.
- `lancollab/common/examples.py`: synthetic sample messages including `FILE_ACK`, new `CHAT_ACK`, presence fields.
- `tests/fixtures/sample-messages.json`: test samples, no real passwords.
- `lancollab/common/socket_transport.py`: TV2 implementation TODO.
- `lancollab/server/application.py`: TV1 handler TODO.
- `lancollab/client/application.py`: TV3 API TODO.

## Independent team work

**TV1**: Server and session registry against fake messages. Model atomic presence snapshots, generations on login takeover, actor-authorized file transfer and nonblocking queues. Do not block readers waiting for queue capacity.

**TV2**: Implement actual TCP length-prefixed JSON codec, schema/type/range validation, TLS wrapper and certificate tooling. Implement FILE_ACK cumulative receiver credit, strict `FILE_CHUNK`/`FILE_END` ordering and state machine in real network integration.

**TV3**: Client/CLI against FakeTransport, UDP DISCOVER and HEARTBEAT_ACK RTT calculation, real network integration and tests; show receiver-confirmed transfer progress.

## Mock-only smoke checks

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m scripts.mock_contract_demo
python -m lancollab.server --help
python -m lancollab.client --help
```

Do not report mock smoke checks as Checkpoint A/B/C successes. No actual application has been implemented at this point.
