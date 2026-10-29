# TV3 tests and acceptance boundaries

```mermaid
flowchart TB
    CODE["TV3 CLI UDP and file modules"]
    UNIT["Deterministic local unit tests - implemented"]
    FAKE["Simulated 20 clients and file relay - implemented"]
    REAL["3 client authenticated TCP TLS - blocked by TV1 TV2"]
    STRESS["20 client LAN and 10 MiB - harness ready, execution pending"]
    REPORT["Logs and screenshots for report - pending real runs"]
    CODE --> UNIT
    CODE --> FAKE
    UNIT --> REAL
    FAKE --> REAL
    REAL --> STRESS
    STRESS --> REPORT
```

No unit, fake, static-check or green CI result can be presented as actual TCP/TLS integration or LAN load evidence. The opt-in harness `scripts/tv3_real_smoke.py` runs only after verified TV2 transport and TV1 server/account setup.