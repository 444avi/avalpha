# avalpha — cloud Linux deployment (systemd)

Target: any systemd-based Linux VM (EC2, GCE, Azure VM, DigitalOcean droplet,
Lightsail, …). Assumes a service account `avalpha` and the repo at
`/opt/avalpha`; adjust `User=`/`ExecStart=` in the unit files if yours differ.

A single small instance is plenty — the workload is a handful of short HTTP
polls plus one long-running worker.

## 1. Durable storage

The database is the archive and must survive restarts and redeploys. Put it on
a persistent disk (an attached/managed volume), **not** ephemeral instance
storage. `config.toml` `[storage]` points there:

```toml
[storage]
db_path = "/opt/avalpha/data/avalpha.db"
digest_dir = "/opt/avalpha/data/digests"
```

If the persistent volume is mounted elsewhere (e.g. `/data`), point these at it.

## 2. Install

```bash
sudo useradd --system --home /opt/avalpha --shell /usr/sbin/nologin avalpha
sudo mkdir -p /opt/avalpha && sudo chown avalpha:avalpha /opt/avalpha

# WeasyPrint native deps (Debian/Ubuntu shown; use the distro equivalents):
sudo apt update
sudo apt install -y python3-venv libpango-1.0-0 libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info

# As the avalpha user, clone/copy the repo to /opt/avalpha, then:
cd /opt/avalpha
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.toml.example config.toml   # then edit storage paths + email
mkdir -p /opt/avalpha/data/digests
```

## 3. WeasyPrint smoke test

WeasyPrint needs native pango/cairo libs; confirm the render path works before
enabling the digest timer (nothing else depends on it):

```bash
.venv/bin/python -c "
from weasyprint import HTML
HTML(string='<h1>avalpha render test</h1>').write_pdf('/tmp/wp-test.pdf')
print('WeasyPrint OK')
"
```

If it fails, fix the native packages above. On a minimal container base image
you may also need `fonts-dejavu-core` (or another font package) for text to
render.

## 4. Secrets

```bash
sudo mkdir -p /etc/avalpha
sudo tee /etc/avalpha/env > /dev/null <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
FINNHUB_API_KEY=...
FRED_API_KEY=...
GMAIL_APP_PASSWORD=...
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
AVALPHA_CONTACT_EMAIL=you@example.com
EOF
sudo chmod 600 /etc/avalpha/env
sudo chown avalpha:avalpha /etc/avalpha/env
```

If your cloud offers a secrets manager, prefer templating this file from it (or
injecting the vars another way) over storing long-lived keys on disk.

- **Finnhub**: free-tier key from https://finnhub.io — the prices collector uses
  the `/quote` endpoint, the calendar collector `/calendar/earnings` and
  `/stock/profile2` (60 calls/min free tier).
- **FRED**: free, instant key from https://fredaccount.stlouisfed.org/apikeys —
  the calendar collector pulls forward, reschedule-aware macro release dates from
  the FRED API (CPI, PPI, jobs, GDP, PCE, retail).
- **Reddit**: a "script" app at https://www.reddit.com/prefs/apps (OAuth).
- **Gmail**: enable 2FA on the account, then generate an app password at
  https://myaccount.google.com/apppasswords. The sending address is
  `config.toml` `[email] sender` (the account that owns the app password).
- **AVALPHA_CONTACT_EMAIL** goes into the SEC EDGAR `User-Agent`; requests
  without a real contact email are blocked outright.

## 5. Seed the watchlist

```bash
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha add NVDA --weight 12
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha list
```

Do this before enabling timers — collectors no-op on an empty watchlist, but
review the enrichment output while you're at the keyboard.

## 6. Enable units

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  avalpha-edgar.timer avalpha-ir.timer avalpha-gnews.timer \
  avalpha-reddit.timer avalpha-prices.timer avalpha-calendar.timer \
  avalpha-matcher.timer avalpha-scorer.service avalpha-digest.timer
```

Timers fire at each source's *fastest* cadence; the market-state due-check in
the code enforces the spec's cadence table (regular / after-hours / overnight,
holidays skipped), so quiet-hours firings exit immediately.

**Timezone:** the digest timer uses `OnCalendar=... America/Los_Angeles` for the
6am PT send, and market-state boundaries are computed in Pacific regardless of
the host clock — so you don't need to set the VM's timezone. (Do keep the clock
synced via NTP/chrony, which cloud images enable by default.)

## 7. Verify

```bash
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha status   # collectors show recent 'last ok'
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha test-digest
journalctl -u avalpha-scorer -n 50                      # scorer draining cleanly
systemctl list-timers 'avalpha-*'
```

After 24 hours: `avalpha status` again — queues near zero, no persistent error
counts — then confirm the 6am email arrived.

## Debugging

- `avalpha status` — last run + error per collector, queue depths, last digest.
- `avalpha run-collector edgar --force` — run any stage by hand.
- `avalpha run-matcher`, `avalpha run-scorer --once` — the LLM stages.
- `journalctl -u avalpha-<unit>` — systemd owns all process logging.
- The pipeline is restartable by design: items land raw in SQLite before any
  interpretation, so an API outage just leaves work queued.
