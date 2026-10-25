# Contract fixture directory (not integration tests)

`sample-messages.json` contains synthetic v1.2 cases shaped as `{direction, message}`. The direction is test metadata; only `message` is the wire envelope. `example_cases()` uses the same convention and `example_messages()` keeps the earlier smoke API. Fixtures never carry real credentials. They intentionally do **not** exercise TCP, TLS, authentication, cryptographic security or file integrity. Those require production code and real socket tests.

The declarative field catalog is `lancollab/common/message_schema.py`. Implementers must add runtime validation of field **types**, ranges, nested values, conditional presence fields, extra keys, directions and session/state authorization.
