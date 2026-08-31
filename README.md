# avalpha

Portfolio monitoring service. Watches public information about a watchlist of
held tickers and emails a morning PDF digest — one page per holding. Phase 1:
no instant alerts, batch delivery only.

Pipeline (each stage has one job; failures are diagnosable per stage):

```
sources → collectors → item store → matcher → scorer → morning PDF
```

- **Collectors** (SEC EDGAR, company IR feeds, Google News RSS, Reddit, Stooq
  prices): fetch, normalize, write raw rows. No LLM, no judgment.
- **Matcher**: links items to tickers. CIK-exact for filings; cheap keyword
  pass + Haiku confirm pass for everything else. Never matches ticker symbols.
- **Scorer**: Sonnet, structured JSON verdicts with a required `mechanism`
  field. Append-only per prompt version — `avalpha replay` re-scores history.
- **Digest**: 6am PT, WeasyPrint PDF via Gmail SMTP. Quiet days say so explicitly.

## Quick start (dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp config.toml.example config.toml           # edit paths + email
export ANTHROPIC_API_KEY=... AVALPHA_CONTACT_EMAIL=you@example.com
.venv/bin/avalpha add NVDA --weight 12
.venv/bin/avalpha run-collector edgar --force
.venv/bin/avalpha run-matcher
.venv/bin/avalpha run-scorer --once
.venv/bin/avalpha test-digest
.venv/bin/avalpha status
```

Tests: `.venv/bin/python -m pytest`

Deployment:
- **AWS** (EC2 + CloudFormation, recommended): [deploy/aws/README.md](deploy/aws/README.md)
- **Generic Linux VM + systemd**: [systemd/INSTALL.md](systemd/INSTALL.md)
