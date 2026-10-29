# TV3 Issue #7 — implementation and report evidence index

## Scope of this draft PR

**Implemented (transport-independent)**: `CollabClient` state transitions for HELLO/AUTH, request ID correlation, chat payloads and ACK semantics, USER_LIST/PRESENCE_UPDATE, responding to PING, LOGOUT, cleanup, safe console rendering helper, and receive-loop control. `tests/test_client_logic.py` contains 25 deterministic mock tests.

**Not implemented by this PR**: interactive `python -m lancollab.client` CLI, actual TLS certificate verification/bootstrap, real TCP framing/codec (TV2), server behaviors (TV1), file transfer (Issue #8), UDP discovery (Issue #9), three-client/LAN integration or load tests. The `auth_allowed` opt-in is **not** proof of secure transport and may only be used with synthetic credentials in FakeTransport tests until connected to verified TLS.

## Reproduction

```bash
python -m unittest tests.test_client_logic -v
python -m unittest discover -s tests -p "test_*.py" -v
```

A local isolated run using copies of the project's `Envelope` and `FakeTransport` contracts on Python 3.13.5 completed **25/25 tests successfully**. This is **dummy evidence only**; full repository CI and actual socket integration have not been verified at time of authoring. CI should run again on the PR head.

## Report-ready diagrams

- [Client application state transitions](diagrams/client-issue7-state.md) — status and invariant explanations.
- [HELLO/AUTH/chat request correlation](diagrams/client-issue7-requests.md) — request IDs, ACK semantics, failure cases.
- [Receive/send concurrency boundaries](diagrams/client-issue7-concurrency.md) — explicit distinction between implemented components and later integration.
- Existing [transport overview](diagrams/transport-overview.md) and [connection and chat](diagrams/connection-and-chat.md) show the **intended full system**, not proof it is implemented.

## Next engineering steps before marking Issue #7 done

- Integrate TV2's verified TLS socket transport and decode/validation contract; eliminate the caller-supplied security boolean as the production auth gate.
- Implement portable command parser and interactive entry point, with input and receive execution separated.
- Run real server/client integration against TV1/TV2, including timeout, disconnect, takeover, simultaneous chat, malformed frames, PING responsiveness, terminal escape handling.
- Capture actual commands, test environment, logs/screenshots, CI links and independent peer review in Notion. No premature completion checkboxes.

Related: [Issue #7](https://github.com/1K-Keem/lan-collab/issues/7) and [protocol revision 1.2](protocol-spec.md).
