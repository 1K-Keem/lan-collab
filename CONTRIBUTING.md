# Contributing to LAN Collab

## One-week assignment workflow

- Open or claim an Issue. The `TV1`, `TV2`, and `TV3` identifiers are roles, not GitHub accounts.
- Work on a short-lived branch from `main`, for example `feature/server-auth` or `feature/file-transfer`.
- Submit a Pull Request with the issue link (`Closes #N`), test evidence, and an updated README/doc entry for new behavior.
- One other team member reviews schema changes and any work crossing a module boundary.
- Prefer small, frequent integrations over long-lived divergent branches.
- Never commit passwords, private keys, real credentials, or downloaded user files.

## Ownership guide

| Area | Primary reviewer |
| --- | --- |
| `server/` | TV1 |
| `common/`, protocol and file transfer | TV2 |
| `client/`, discovery and test harness | TV3 |
| `docs/`, cross-module changes | Entire team |

## Definition of Done for an Issue

1. Acceptance criteria checked against running behavior or reviewed design.
2. Source follows the shared protocol contract.
3. Test cases and failure paths are documented and run.
4. No secrets committed and no unhandled crashes on invalid input.
5. PR describes what changed and exactly how to reproduce it.

## Commit style

Examples: `feat(server): route global messages`, `test(protocol): cover partial TCP reads`, `docs: add sequence diagrams`.

## Instructor requirements and academic integrity

The assignment document says AI can assist with syntax reference/debugging, and all members must be able to explain their code during the defense. Use this repository to plan and collaborate; implement graded functions yourselves. Instructor clarification is needed on conflicting public/private repo instructions.
