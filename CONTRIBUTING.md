# Contributing

1. Work on a short-lived branch and describe the concrete change in a pull request.
2. Keep shared protocol changes consistent across client, server, schema and tests.
3. Update the user guide when commands or visible behavior change.
4. Run the checks below and document any manual verification or limitations.
5. Ask another team member to review changes, particularly authentication, file
   lifecycle and concurrency behavior.

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -p "test_*.py"
```

Tests use synthetic accounts and temporary certificates. `tests/fake_transport.py`
provides deterministic in-memory messages for client logic; socket tests verify
framing, TLS and real connection behavior. Shared synthetic messages live in
`tests/fixtures/sample-messages.json`. Keep test helpers out of the runtime package.

For multi-client/file checks and manual LAN evidence, follow
[acceptance.md](docs/acceptance.md). Do not commit private keys, passwords, account
databases, tokens or downloaded files. The public server certificate is the
intentional exception: coordinate its distribution with the group server's identity.

Use concise commit messages such as `Fix file cancellation cleanup` or
`Document client certificate setup`. Follow the course's academic integrity rules;
each contributor should understand and be able to explain the code they submit.
