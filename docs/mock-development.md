# Parallel development with dummy transports

This repository includes **interfaces and in-memory test doubles only**, not working chat/file/network implementations. These deliberately separate the jobs of TV1/TV2/TV3.

## Contract files

- `lancollab/common/contracts.py`: `Envelope`, `MessageTransport`, `ProtocolCodec`, `SessionIdentity`.
- `lancollab/common/fake_transport.py`: paired queue-backed `FakeTransport` for tests; **no TCP/UDP/TLS**.
- `lancollab/common/examples.py`: representative typed fixture messages.
- `lancollab/common/socket_transport.py`: production `SocketTransport` TODO for TV2.
- `lancollab/server/application.py`: server handler TODO for TV1.
- `lancollab/client/application.py`: client API TODO for TV3.
- `scripts/mock_contract_demo.py`: smoke checks synthetic request/response messages. It is **not** a network demo.

## TV1 workflow

Write `CollabServer.handle_message(message, session)` without opening sockets, using deterministic input from `example_messages()` or `FakeTransport`. Once `SocketTransport` is supplied by TV2, wire the same handler into a per-client reader loop. Test auth and private/global routing with fake authenticated sessions and real sockets later.

## TV2 workflow

Implement `SocketTransport` and `ProtocolCodec`: exact-byte TCP frame receiving; JSON serialization; frame-size checks; a serialized writer. Independently test file chunking/reassembly and TLS certificate validation. Keep the public methods stable.

## TV3 workflow

Implement `CollabClient` against `MessageTransport` (initially `FakeTransport`) and a CLI. Use fixtures as synthetic server events; do not wait for server. Write UDP discovery client and test harness separately. Later swap the fake transport for a real TLS socket implementation.

## Smoke commands (now)

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m scripts.mock_contract_demo
python -m lancollab.server --help
python -m lancollab.client --help
```

These commands **only** check interfaces/mocks/skeleton entry points until actual networking handlers are implemented. They must not be presented as completed rubric functionality.
