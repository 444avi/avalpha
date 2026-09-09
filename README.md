# avalpha

Portfolio monitoring service. Watches public information about a watchlist of
held tickers and emails a morning PDF digest — one page per holding. Phase 1:
no instant alerts, batch delivery only.

The web console gives each verified email one isolated portfolio while sharing
company metadata and collected/scored public information globally. The
database-backed administrator can inspect other portfolios read-only from
`/admin`; scheduled digest runs build and email separate PDFs for every owner.

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
- **Digest**: 6am PT, WeasyPrint PDF via Gmail SMTP. One page per holding, plus
  a cover with portfolio themes, a 7-day catalyst calendar, and a post-event
  "what happened" macro block — released figures for any Fed/CPI/PCE/jobs/PPI/GDP
  event in the window (actuals from FRED) with model analysis, and a per-holding
  earnings beat/miss (EPS vs estimate). Quiet days say so explicitly. Outcomes
  are computed at build time, not stored — see [docs/calendar.md](docs/calendar.md) §12.

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
