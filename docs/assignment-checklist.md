# Assignment checklist

Source: [assignment requirements](../assignment%20requirements.md). Keep this
checklist with the submission; a passing unit test does not replace a recorded demo.

| Step | Milestone | Evidence to include |
| --- | --- | --- |
| 1 | Protocol and setup | [Wire format and states](protocol-spec.md), architecture and diagrams |
| 2 | TCP handshake | Connection and TLS logs, followed by an authentication response |
| 3 | Concurrency | Multiple clients connecting/disconnecting; [load results](acceptance.md) |
| 4 | Authentication and state | Login/approval, user list, IP/source port and takeover checks |
| 5 | Text routing | Global/private messages reaching the correct users |
| 6 | File transfer | Consent, chunks, reassembly and identical received SHA-256 |
| 7 | Resilience | Timeout, broken connection, cancellation and successful reconnect |
| 8 | Security | Rejected untrusted certificates and encrypted traffic capture |
| 9 | Interface | Chat, online list, network status and file progress in actual terminals |
| 10 | Quality and testing | Unit/integration results, lint/format checks and two-host tests |

## Submission deliverables

- [ ] Client/server source and shared protocol code.
- [ ] README with installation, server startup and client connection instructions.
- [ ] Technical report PDF with system architecture and packet/message structures.
- [ ] Message and file-transfer sequence diagrams.
- [ ] Approximately one-page Challenges & Solutions reflection.
- [ ] Actual test results and representative screenshots/logs.
- [ ] Team review and ability to explain the submitted code.

The report source is [technical-report.md](technical-report.md). Generate its PDF:

```bash
python -m pip install -r requirements-report.txt
python -m scripts.build_report
```

Use [acceptance.md](acceptance.md) as the single runbook for test commands,
performance targets and remaining manual evidence. Confirm the official submission
date with the instructor: the brief says “Sunday, November 17, 2026”, but that date
falls on a Tuesday. The brief permits a ZIP file or private repository link.
