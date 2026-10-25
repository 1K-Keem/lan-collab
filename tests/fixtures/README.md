# Contract fixture directory (not integration tests)

`sample-messages.json` contains synthetic envelopes for revised v1.1 fields. Fixtures never carry real credentials. They intentionally do **not** exercise TCP, TLS, authentication, cryptographic security or file integrity. Those require production code and real socket tests.

The declarative field catalog is `lancollab/common/message_schema.py`. Implementers must add runtime validation of field **types**, ranges, nested values, conditional presence fields, extra keys, directions and session/state authorization.
