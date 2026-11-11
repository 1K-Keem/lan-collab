# Protocol fixtures

`sample-messages.json` is the single source of synthetic message cases. Each named
case has `direction` (`C` or `S`) and `message`; only `message` is the wire envelope.
`tests/examples.py` loads these into `Envelope` objects for codec and contract tests.

Fixtures contain no real credentials. Round-trip codec tests exercise the production
validator; real sockets, TLS and file integrity are covered separately by integration
tests. Runtime field definitions live in `lancollab/common/message_schema.json`.
