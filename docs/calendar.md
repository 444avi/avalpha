# avalpha Calendar — Design Doc

Status: proposed · Owner: TSF · Target: Phase 2 feature on top of the existing
collector → matcher → scorer → digest → web pipeline.

## 1. Purpose & scope

A calendar of **important upcoming dates** for portfolio holdings and the broad
market: when a catalyst is coming, not what it says.

**Scope principle — a scheduling layer, not a collector.** The calendar ingests
one small structured fact per event ("AAPL reports 2026-10-28, after close").
It does **not** fetch, summarize, or score the content of those events — that is
the job of the existing pipeline (EDGAR / IR / GNews / Reddit → matcher →
scorer), which runs when the date arrives. The calendar's payoff is to
*anticipate* dates and, secondarily, to *escalate polling* around them via the
`earnings_escalated()` hook that already exists in `market_state.py` but is
currently a stub.

Keep that line firm: knowing *when* is in scope; knowing *what happened* is the
existing pipeline's job.

## 2. Finnhub free-tier findings (probed against the live key)

Verified 2026-09-01 against the production key in `.env`:

| Endpoint | Free? | What we take from it |
| --- | --- | --- |
| `/calendar/earnings` | ✅ | Earnings **date**, `hour` (`amc`/`bmo`), quarter/year, EPS & revenue estimates |
| `/stock/profile2` | ✅ | `ipo` date (→ lockup), `finnhubIndustry` (→ bio auto-detect), `shareOutstanding` |
| `/stock/recommendation` | ✅ | Analyst *rating trend* buckets — **not** an event date; not used by the calendar |
| `/stock/earnings` | ✅ | Past earnings surprises — context only, no future date |
| `/company-news` | ✅ | Discovery source for product launches / analyst days (Phase 3) |
| `/calendar/economic` | ❌ 403 | Macro comes from the **FRED API + Fed FOMC page** (free, self-sustaining — §5) |
| `/stock/dividend`, `/stock/split` | ❌ 403 | Dividends / splits **out of scope** |
| `/press-releases` | ❌ 403 | Use `/company-news` or the IR feed instead |

Consequences that shaped this design:

- **Earnings dates are a clean, free API pull** — no EDGAR estimation needed to
  seed them. The `amc`/`bmo` field also tells the escalation hook *when* on the
  day to ramp EDGAR/IR polling.
- **Macro is premium**, so it lives in a hand-maintained file — the same pattern
  already used for `MARKET_HOLIDAYS` in `market_state.py`. For a single-user tool
  this is more reliable than an API and costs nothing.
- **Bio detection is free and trivial** via `profile2.finnhubIndustry`
  (`"Biotechnology"` for MRNA, `"Semiconductors"` for NVDA).

## 3. Event set

### Company-specific — Finnhub free tier (built pipeline)

| Kind | Source | Notes |
| --- | --- | --- |
| `earnings` | `/calendar/earnings`, confirmed via 8-K | The #1 catalyst; also the escalation trigger |
| `ipo_lockup` | computed from `profile2.ipo + 180d` | Created **only** if IPO was within the last ~6 months |

### Company-specific — discovered or manual (no free feed exists)

| Kind | How | Notes |
| --- | --- | --- |
| `analyst_day` | IR feed / `company-news` / 8-K, or manual | `/stock/recommendation` is rating trend, **not** a date |
| `pdufa` | manual / discovered | **Auto-enabled** only for holdings whose `finnhubIndustry` ∈ {Biotechnology, Pharmaceuticals}. Binary catalyst where relevant |
| `product_launch` | discovered "if noticed" from items already collected | Phase 3 extractor; else manual |

### Macro — self-sustaining via FRED + Fed (`/calendar/economic` is premium)

**Tier A (always show):** `fomc`, `cpi`, `pce`, `jobs` (nonfarm payrolls),
`ppi`, `gdp`.

**Tier B (web tab only, collapsed by default — never in the digest):**
`fomc_minutes`, `retail_sales`, `ism` (mfg + services), `sentiment` (UMich +
Conf. Board), `beige_book`, `fed_speak` (testimony / Jackson Hole),
`jobless_claims` (off by default even within B).

**Situational (off unless relevant):** foreign central banks (ECB/BOJ) + China
data if a holding is exposed; debt-ceiling / shutdown deadlines when one is live.

Explicitly **excluded** (per scoping): options expiration / quad-witching,
quarter/month-end, earnings-season kickoff, EIA/OPEC, Treasury auctions,
dividends, splits, AGM, index rebalancing, SEC filing deadlines.

### Manual

`manual` — member-entered events (expansion openings, private meetings, and any
discovered event confirmed by hand). The escape hatch for everything no feed
covers.

## 4. Data model

New table, following existing conventions (UTC ISO-8601 strings, `meta_json`,
`source` provenance). Schema bumps `SCHEMA_VERSION` 2 → 3.

```sql
CREATE TABLE calendar_events (
    id            INTEGER PRIMARY KEY,
    ticker        TEXT,               -- NULL = macro / market-wide. Not FK-constrained
                                      -- (macro rows are tickerless; keep events for
                                      -- deactivated holdings).
    kind          TEXT NOT NULL,      -- earnings | ipo_lockup | analyst_day | pdufa |
                                      -- product_launch | fomc | cpi | pce | jobs | ppi |
                                      -- gdp | fomc_minutes | retail_sales | ism |
                                      -- sentiment | beige_book | fed_speak |
                                      -- jobless_claims | manual
    title         TEXT NOT NULL,
    event_date    TEXT NOT NULL,      -- YYYY-MM-DD, local calendar date (the "when")
    event_at      TEXT,               -- UTC ISO instant, only for timed events
    tz            TEXT,               -- e.g. America/New_York (macro releases are ET)
    is_timed      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','confirmed','tentative','passed','cancelled')),
    source        TEXT NOT NULL,      -- finnhub | edgar | ir | curated | manual | computed
    source_ref    TEXT,              -- accession no. / url / endpoint
    confidence    TEXT CHECK (confidence IN ('high','medium','low')),
    fiscal_period TEXT,              -- e.g. 2026Q4, for earnings dedup
    dedup_key     TEXT NOT NULL UNIQUE,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_calendar_date   ON calendar_events (event_date);
CREATE INDEX idx_calendar_ticker ON calendar_events (ticker, event_date);
```

Plus one column on `watchlist` to persist the industry for bio-gating:

```sql
ALTER TABLE watchlist ADD COLUMN industry TEXT;  -- from profile2.finnhubIndustry, set at enrich time
```

### Dedup / merge key (the important part)

All writes are **upserts on `dedup_key`**, never blind inserts, so the same
event arriving from multiple refreshes (or later from an 8-K) collapses into one
row and is *upgraded in place*.

| Kind | `dedup_key` |
| --- | --- |
| earnings | `earnings:<TICKER>:<fiscal_period>` e.g. `earnings:AAPL:2026Q4` |
| ipo_lockup | `lockup:<TICKER>` |
| macro | `macro:<kind>:<event_date>` e.g. `macro:cpi:2026-09-11` |
| manual | `manual:<uuid>` (generated once at creation) |
| discovered | `disc:<kind>:<TICKER>:<event_date>` |

Upsert rule: if `dedup_key` exists, `UPDATE` `event_date`, `event_at`, `status`,
`updated_at`, and merge `meta_json`; keep `created_at`. Never demote a
`confirmed`/`manual` row back to `scheduled` on a routine refresh.

## 5. Data sources & field mapping

### Earnings — `/calendar/earnings`

Per active holding (or one ranged call, filtered client-side). Response item:
`{symbol, date, hour, quarter, year, epsEstimate, revenueEstimate, ...}`.

- `dedup_key = earnings:{symbol}:{year}Q{quarter}`
- `event_date = date`
- `is_timed = 1`; derive an approximate `event_at`/`tz` from `hour`:
  `amc` → ~16:05 ET, `bmo` → ~07:00 ET. **Approximate — do not present as exact.**
- `status = 'scheduled'` (Finnhub gives no confirmed flag; see §6)
- `meta_json`: `{hour, epsEstimate, revenueEstimate}` for display
- `source = 'finnhub'`, `confidence = 'medium'`

### IPO lockup — `/stock/profile2.ipo`

At enrich time (or in the calendar collector): read `ipo`. If
`today - ipo <= ~183 days`, create:

- `kind = 'ipo_lockup'`, `dedup_key = lockup:{ticker}`
- `event_date = ipo + 180 days`, `status = 'scheduled'`, `source = 'computed'`
- `confidence = 'low'` — lockup terms vary (90–180d); label it an estimate in UI.
  Older IPOs create no row.
- **Editable per holding:** the computed row is a normal calendar event, so the
  member can correct `event_date` by hand via the manual CRUD (§ below) when the
  real terms are known. A hand-edit sets `source='manual'`/`confidence='high'`
  and is preserved across refreshes (never demoted — see §4 upsert rule).

### Bio gate — `/stock/profile2.finnhubIndustry`

Persist to `watchlist.industry` at enrich. If ∈ {`Biotechnology`,
`Pharmaceuticals`, drug-manufacturer variants}, the holding is bio: the UI offers
a PDUFA/readout quick-add and the Phase 3 discovery extractor looks for
readout/PDUFA language. No automatic PDUFA feed exists on free tier.

### Macro — self-sustaining (no hand-maintained date file)

The macro calendar must **not** be a static list someone re-types each year.
Two reasons a static file fails: (1) it goes stale every January, and (2) dates
get **rescheduled** mid-year — the Oct–Nov 2025 government shutdown pushed
several 2026 releases (CPI Feb 11→13, PCE/GDP shuffled). Only a live source stays
correct. So macro dates are generated three ways, all rechecked on the daily
collector run:

**(a) FRED API — statistical releases (the source of truth).** The St. Louis
Fed's FRED API is free (one API key) and publishes **forward, reschedule-aware**
release dates. Note: the FRED *website* and BLS both 403 automated fetches — use
the **API host** `api.stlouisfed.org`, not scraping. Endpoint:

```
GET /fred/release/dates?release_id={id}&include_release_dates_with_no_data=true
    &api_key={FRED_API_KEY}&file_type=json
```

Resolve `release_id` **by name** once via `GET /fred/releases` and cache the
map (more robust than hard-coding ids). Releases we pull:

| `kind` | FRED release name | tier |
| --- | --- | --- |
| `cpi` | Consumer Price Index | A |
| `ppi` | Producer Price Index | A |
| `jobs` | Employment Situation | A |
| `gdp` | Gross Domestic Product | A |
| `pce` | Personal Income and Outlays | A |
| `retail_sales` | Advance Monthly Sales for Retail and Food Services | B |
| `jobless_claims` | Unemployment Insurance Weekly Claims | B |

(Jobs is **not** rule-derivable — 2026 has it on Feb 11 (a Wednesday) and Jul 2
(a Thursday), not "first Friday" — which is exactly why it comes from FRED.)

**(b) Fed FOMC page — meeting dates.** FRED has no FOMC *meeting* calendar, so
fetch `federalreserve.gov/monetarypolicy/fomccalendars.htm` (fetchable with a
normal User-Agent, same as the EDGAR collector) and parse the meeting rows. The
Fed publishes ~2 years ahead, so this self-sustains too. `event_date` = the
second (decision) day; `event_at` = 14:00 ET.

**(c) Rule-derived — from the FOMC anchors and fixed calendar rules.** No fetch
needed, self-sustaining forever:

- `fomc_minutes` — FOMC decision day **+ 21 days** (always a Wednesday), 14:00 ET.
- `beige_book` — the Wednesday **two weeks before** each FOMC meeting.
- `ism` (Tier B) — mfg = 1st business day, services = 3rd business day, 10:00 ET.
- `sentiment` (Tier B) — Conf. Board = last Tuesday; UMich prelim = 2nd Friday,
  final = 4th Friday.

(`fed_speak` isn't on a fixed schedule — add those as `manual` events.)

Each generated event upserts with `dedup_key = macro:{kind}:{date}`,
`ticker = NULL`, `source = 'fred' | 'fed' | 'derived'`, `tz = 'America/New_York'`,
`status = 'scheduled'`, `confidence = 'high'`.

**Graceful fallback.** If FRED or the Fed page is unreachable on a run, the
collector logs the failure via `base.run()` (health tile goes red) and falls
back to a small **hard-coded anchor table for the current cycle** so the calendar
is never empty. This table is a safety net, not the primary source — FRED
overwrites it on the next successful run. Verified real values to seed it:

```python
# avalpha/calendar_macro.py — FALLBACK ONLY. Live dates come from FRED + the Fed.
# Verified 2026-09-01 against federalreserve.gov, BLS, and the usinflationcalculator
# mirror of the BLS CPI schedule.

# FOMC decision days (2nd day of each meeting), 14:00 ET.
FOMC_DECISION_DAYS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]
# CPI, 08:30 ET (date → reference month).
CPI_2026 = [
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]
# Employment Situation (jobs), 08:30 ET.
JOBS_2026 = [
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03",
    "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]
# fomc_minutes and beige_book are DERIVED from FOMC_DECISION_DAYS, not stored here.
# PPI / GDP / PCE / retail have no fallback table — they come from FRED (their
# 2026 dates were disrupted by the 2025 shutdown, so a hand-copy would mislead).
```

## 6. Earnings lifecycle: scheduled → confirmed → escalation

1. **Seed (Phase 1).** Daily calendar collector pulls `/calendar/earnings` and
   upserts each holding's next earnings row as `status='scheduled'`, refreshing
   `event_date` if Finnhub moved it.
2. **Confirm (Phase 2).** When the EDGAR collector ingests an **8-K item 2.02**
   for a holding within its pre-earnings window, upgrade the matching
   `earnings:{ticker}:{period}` row to `status='confirmed'` and set `event_at`
   from the filing. This reuses items the pipeline already collects — no new feed.
3. **Escalate (Phase 2).** Make `earnings_escalated()` real. Because collectors
   poll **globally** (not per-ticker), the predicate is *"does any active holding
   have a confirmed earnings (or PDUFA) event within the next 48h?"*:

   ```python
   def earnings_escalated(conn, now_utc) -> bool:
       # any active holding with an earnings/pdufa event in [now, now+48h]
   ```

   Wire it into the collector run path so `poll_interval(..., escalated=True)`
   drops EDGAR to 15s / IR to 30s in the window — the loop `market_state.py`
   was already written for.
4. **Sweep.** A daily step marks `event_date < today` rows `status='passed'`
   (kept, not deleted — see retention).

## 7. Surfacing ("alert me it's an important date")

**No push/email alerts.** Decision: catalysts are surfaced by being *visible*,
not by notifying. Two surfaces:

1. **Its own `Calendar` tab** — the primary home, a first-class nav item
   alongside Dashboard / Digests. An **agenda list grouped by week** (fits the
   dense dark console better than a month grid), covering all event kinds for
   active holdings plus macro. **Tier B macro** sits under a **collapsed "More
   macro" toggle** here and appears *nowhere else*. Secondary touches on the
   existing Dashboard: an "Upcoming — next 7 days" strip and a small per-row
   badge ("Earnings in 3d") that deep-links into the tab.

   **Match the existing UI — reuse, don't invent.** The tab must look like the
   rest of the console (see `web/static/styles.css`, `templates/base.html`,
   `dashboard.html`): extend `base.html`, add the nav link in the `.nav-links`
   block with the same `active` logic. Reuse the existing classes verbatim —
   `.hero`/`.eyebrow`/`.stat-row` for the header, `.block`/`.block-head`/
   `.section-label` for each week group, `.table-scroll`+`.grid`/`.grid.compact`
   for the agenda rows (tickers in `.tkr`, dates right-aligned `.num`), `.tag`
   for status chips (`est.`/`confirmed`), `.tile`/`.tiles` if a macro summary row
   is wanted, the `<details class="jobs">` disclosure pattern for the "More
   macro" (Tier B) collapse, `.btn`/`.mini` for manual-event add/edit/delete, and
   `.flash` for confirmations. Fonts and tokens come from `:root` (Space Grotesk
   display face, `--gold` accent for high-signal/near-term items, `--pos`/`--neg`
   already defined). No new colors, fonts, or component styles — a new event kind
   is a new row in an existing table, not a new visual language.
2. **Daily digest.** A "Catalysts — next 7 days" block in `digest/build.py` +
   template — company events + **Tier A macro only** (no Tier B). This rolling
   7-day window is the de-facto lead-time heads-up, which is why no separate
   push alerts are needed.

Note: the **day-of escalation** in §6 is a polling-cadence change, *not* a user
alert — it stays regardless of this decision.

Relevance: company events only for **active** holdings; macro always; passed
events hidden by default.

3. **`.ics` subscription feed (Phase 3).** Read-only, token-guarded URL that
   bypasses Cloudflare Access, emitting **date + title only** (no scores /
   summaries) so events land in the member's real calendar. Per-member revocable
   token; deferred to Phase 3.

## 8. Integration points (existing code)

- **`schema.sql` + `db.py`** — add `calendar_events` and the `watchlist.industry`
  column to `schema.sql`; bump `SCHEMA_VERSION` to 3 and add a `_MIGRATIONS[3]`
  entry (idempotent `CREATE TABLE IF NOT EXISTS` + guarded `ALTER TABLE`).
- **`collectors/__init__.py`** — add `"calendar"` to `SOURCES` and a dispatch
  branch; the new `collectors/calendar.py` exposes `collect(config, conn)` and is
  wrapped by the existing `base.run()` (free failure isolation + `collector_runs`
  logging + a Pipeline Health tile, like every other source). It does three
  things per run: pull earnings + profile2 from Finnhub (mirror `prices.py`,
  including its 401/403/429 handling), pull macro from FRED + the Fed FOMC page
  (§5), and upsert everything by `dedup_key`.
- **`config.py` + `.env`** — add a `fred_api_key` property mirroring
  `finnhub_api_key` ([config.py:40](../avalpha/config.py)), and `FRED_API_KEY` to
  `.env` / `.env.example` and the systemd `EnvironmentFile`. FRED keys are free
  and instant from `fredaccount.stlouisfed.org`. This is the one new secret the
  feature needs; no config knob is required for macro dates.
- **`market_state.py`** — add a `"calendar"` row to `INTERVALS` (daily cadence,
  e.g. 43200s in every state); implement `earnings_escalated()`; compute
  `escalated` for edgar/ir in the run path.
- **`web/queries.py`** — `upcoming_events()`, `events_for_ticker()`,
  `calendar_agenda()`; extend `health()` so the calendar collector shows a tile.
- **`web/app.py` + templates** — `/calendar` route, dashboard strip, per-row
  badge, and manual-event CRUD routes (add/edit/delete).
- **`enrich.py`** — persist `profile2.finnhubIndustry` → `watchlist.industry`.
- **`digest/build.py`** — catalysts section.
- **`systemd/`** — `avalpha-calendar.service` + `.timer` (daily), mirroring the
  existing collector units.

Rate limits: free tier is 60 calls/min. Cost is ~2 calls/holding/day
(earnings + profile2) — negligible; batch politely.

## 9. Considerations & edge cases

- **Earnings dates drift.** Refresh daily and **upsert**, never insert. Finnhub
  gives no confirmed flag → derive confirmation from the 8-K (§6); until then
  show "scheduled," not a hard promise.
- **Time precision.** `amc`/`bmo` and macro release times are ET and approximate;
  store `event_at` + `tz` but present timed events honestly ("after close", not
  "16:05:00").
- **Lockup is an estimate.** 180d is conventional; terms vary. Label it; only
  create rows for recent IPOs.
- **Macro file must be refreshed yearly** — the staleness guard (§5) surfaces it.
- **Escalation is global**, not per-ticker, because collectors poll globally.
- **Retention.** Keep `passed` events — they enable a Phase 3 correlation of
  catalyst → price move / scorer output (did the catalyst you watched actually
  move the name?).
- **Timezones.** DB stays UTC (existing convention); `event_date` is the local
  calendar date, `event_at`/`tz` carry the instant for timed events.
- **Failure isolation.** The calendar collector runs under `base.run()`, so any
  Finnhub outage is logged to `collector_runs` and shows red on its health tile
  without touching the rest of the pipeline.

## 10. Phasing

**Build scope: Phase 1 and Phase 2 together, as the initial delivery.** Phase 3
is deferred. The two immediate phases are kept as milestones (build in this
order — Phase 1 stands alone if Phase 2 slips), not as separate releases.

- **Phase 1 (milestone 1).** `calendar_events` + `watchlist.industry` migration
  (schema v3); `config.fred_api_key` + `FRED_API_KEY`; `collectors/calendar.py`
  (earnings via Finnhub, IPO lockup via profile2, macro via FRED + Fed page + the
  derived rules, with the hard-coded anchor fallback); the Calendar web tab
  (reusing existing UI classes, §7) + dashboard strip + per-row badge;
  manual-event CRUD; health tile; systemd `avalpha-calendar` service + timer.
  *Delivers: dates populated and on screen, plus manual entry.*
- **Phase 2 (milestone 2).** 8-K item-2.02 earnings confirmation upgrade
  (scheduled→confirmed, using the `meta.form`/`meta.items` EDGAR already stores);
  wire `earnings_escalated()` into the run path so EDGAR/IR ramp polling in the
  48h window; digest "Catalysts — next 7 days" block (company + Tier A macro);
  bio-gated PDUFA quick-add. No push/email alerts (§7).
  *Delivers: dates get accurate on their own and drive the pipeline.*
- **Phase 3 (deferred — not in the initial build).** Discovered analyst-day /
  product-launch extractor over existing items; token-guarded `.ics` feed;
  post-hoc catalyst ↔ price/score correlation.

## 11. Resolved decisions

Settled 2026-09-01:

1. **No push/email alerts.** Catalysts are surfaced by being visible on the
   Calendar tab and in the daily digest's rolling 7-day block — good enough; no
   notification subsystem. (Day-of *escalation* in §6 is unaffected — it changes
   polling cadence, not user-facing alerts.)
2. **`.ics` feed: yes, Phase 3** — read-only, token-guarded, date+title only.
3. **Macro Tier B: web tab only, collapsed** — not in the digest, not pushed;
   jobless claims off by default even within Tier B.
4. **IPO lockup: auto 180d (labeled estimate) + editable per holding** via the
   manual CRUD.
5. **Macro is self-sustaining, not a hand-maintained file** — live dates from the
   free FRED API + the Fed FOMC page + calendar rules, with a verified hard-coded
   anchor table (§5) only as an offline fallback. Needs one new free secret,
   `FRED_API_KEY`.
6. **UI matches the existing console** — the Calendar tab reuses the current
   classes/tokens verbatim (§7); no new visual language.
7. **Build Phase 1 + Phase 2 together** as the initial delivery; Phase 3
   deferred (§10).

### Verified during design (2026-09-01)

- EDGAR already persists `meta.form` + `meta.items`, so Phase 2's 8-K item-2.02
  confirmation is buildable ([edgar.py:108](../avalpha/collectors/edgar.py)).
- Finnhub free tier returns `/calendar/earnings` and `/stock/profile2` (incl.
  `ipo` + `finnhubIndustry`); `/calendar/economic`, dividends, splits are 403.
- FOMC 2026–27 dates confirmed from the Fed; CPI/jobs 2026 dates confirmed from
  BLS mirrors. FRED website and BLS 403 automated fetches → use the FRED **API**
  host (`api.stlouisfed.org`), and fetch the Fed page with a real User-Agent
  (as EDGAR does).
- Remaining build-time judgment calls: exact placement of the `escalated`
  computation in the run path; refresh cadence for the calendar timer; test
  coverage. All follow existing repo conventions.
