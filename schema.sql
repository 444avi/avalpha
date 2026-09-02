-- avalpha schema v3. Applied via PRAGMA user_version migrations in db.py.
-- All timestamps are UTC ISO-8601 strings ("YYYY-MM-DDTHH:MM:SSZ").

CREATE TABLE watchlist (
    ticker                TEXT PRIMARY KEY,
    cik                   TEXT NOT NULL,
    legal_name            TEXT NOT NULL,
    aliases_json          TEXT NOT NULL DEFAULT '[]',
    products_json         TEXT NOT NULL DEFAULT '[]',
    executives_json       TEXT NOT NULL DEFAULT '[]',
    ir_feed_url           TEXT,
    ir_feed_status        TEXT NOT NULL DEFAULT 'none' CHECK (ir_feed_status IN ('ok', 'none')),
    weight                REAL NOT NULL DEFAULT 0,
    shares_outstanding    INTEGER,
    enrichment_confidence TEXT CHECK (enrichment_confidence IN ('high', 'medium', 'low')),
    enriched_at           TEXT,
    industry              TEXT,               -- profile2.finnhubIndustry; gates bio (PDUFA) events
    active                INTEGER NOT NULL DEFAULT 1,
    added_at              TEXT NOT NULL,
    deactivated_at        TEXT
);

CREATE TABLE items (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,          -- edgar | ir | gnews | reddit
    source_id    TEXT,                   -- source-native ID (accession no., reddit id, guid)
    url          TEXT NOT NULL,
    url_hash     TEXT NOT NULL UNIQUE,   -- sha256(url), dedup at write time
    title        TEXT NOT NULL,
    raw_text     TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    meta_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_items_fetched ON items (fetched_at);
CREATE INDEX idx_items_source ON items (source, fetched_at);

CREATE TABLE item_matches (
    item_id    INTEGER NOT NULL REFERENCES items (id),
    ticker     TEXT NOT NULL,
    method     TEXT NOT NULL CHECK (method IN ('cik', 'cheap', 'confirm')),
    confirmed  INTEGER NOT NULL DEFAULT 0,
    matched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, ticker)
);
CREATE INDEX idx_matches_ticker ON item_matches (ticker, matched_at);

-- Matcher bookkeeping: an item is recorded here once the matcher has fully
-- processed it (even when it matched zero tickers), so nothing is re-scanned.
CREATE TABLE matcher_done (
    item_id      INTEGER PRIMARY KEY REFERENCES items (id),
    processed_at TEXT NOT NULL
);

-- Append-only. Replay writes new rows at a new prompt_version; old verdicts kept.
CREATE TABLE scores (
    id             INTEGER PRIMARY KEY,
    item_id        INTEGER NOT NULL REFERENCES items (id),
    ticker         TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    materiality    INTEGER NOT NULL,
    direction      TEXT NOT NULL,
    category       TEXT NOT NULL,
    mechanism      TEXT NOT NULL,
    summary        TEXT NOT NULL,
    raw_json       TEXT NOT NULL,
    scored_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_scores_unique ON scores (item_id, ticker, prompt_version);
CREATE INDEX idx_scores_ticker ON scores (ticker, scored_at);

-- Rolling mention counts; baseline corpus for Phase 2 volume-anomaly detection.
CREATE TABLE reddit_mentions (
    ticker       TEXT NOT NULL,
    window_start TEXT NOT NULL,   -- UTC hour bucket
    count        INTEGER NOT NULL,
    PRIMARY KEY (ticker, window_start)
);

CREATE TABLE prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,          -- YYYY-MM-DD (exchange local date)
    open   REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE collector_runs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    ok            INTEGER,
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new     INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE INDEX idx_runs_source ON collector_runs (source, started_at);

CREATE TABLE digests (
    date     TEXT PRIMARY KEY,     -- trading day covered, YYYY-MM-DD
    built_at TEXT NOT NULL,
    sent_at  TEXT,
    pdf_path TEXT NOT NULL
);

-- Web console job runs: on-demand collector/matcher/scorer/digest triggers
-- kicked from the UI. One row per trigger; the runner updates status on finish.
CREATE TABLE IF NOT EXISTS web_jobs (
    id           INTEGER PRIMARY KEY,
    job          TEXT NOT NULL,     -- collector:edgar | matcher | scorer | digest | enrich:TICKER
    status       TEXT NOT NULL,     -- running | ok | error
    triggered_by TEXT,              -- member email from Cloudflare Access
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    output       TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_jobs_started ON web_jobs (started_at);

-- Upcoming catalyst dates for holdings and the broad market. A scheduling
-- layer, not a collector: one small structured fact per event (the "when"),
-- never the content. All writes are upserts on dedup_key (see calendar_store).
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
                                      --   | fred | fed | derived
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
