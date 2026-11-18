-- AlphaLab database schema (protocol v1.0, §9).
--
-- Conventions
--   * Every timestamp is UTC text in exactly this form: 2026-11-16T22:45:00.123Z
--     (always 3 fractional digits and a trailing Z, so text order = time order).
--     Default: strftime('%Y-%m-%dT%H:%M:%fZ','now'). CHECK constraints reject
--     anything else.
--   * Trading / calendar dates are 'YYYY-MM-DD' and are stored separately from
--     timestamps.
--   * asset_class is 'us_equity' (agents 1-15) or 'crypto' (C0, C1; 24/7).
--   * Every data table is APPEND-ONLY (CLAUDE.md rule 2): triggers at the bottom
--     abort any UPDATE or DELETE. Corrections are new rows whose corrects_id
--     points at the original. Only the `agents` reference table is editable.
--   * prices is versioned instead: each fetch inserts new rows, and readers take
--     the latest version fetched at or before their cutoff (point-in-time).
--
-- Safe to run repeatedly: everything is IF NOT EXISTS / INSERT OR IGNORE.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- agents: reference data (the only editable table)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,                -- '1'..'15', 'C0', 'C1'
    name           TEXT NOT NULL,
    asset_class    TEXT NOT NULL CHECK (asset_class IN ('us_equity', 'crypto')),
    kind           TEXT NOT NULL CHECK (kind IN ('rule', 'ai')),
    risk_profile   TEXT NOT NULL CHECK (risk_profile IN ('market', 'medium', 'low', 'high', 'crypto')),
    mirror_account INTEGER CHECK (mirror_account IN (1, 2, 3)),  -- D1; NULL = not mirrored
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                   CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z')
);

INSERT OR IGNORE INTO agents (agent_id, name, asset_class, kind, risk_profile, mirror_account) VALUES
    ('1',  'Benchmark (SPY)',            'us_equity', 'rule', 'market', 1),
    ('2',  'Equal-weight hold',          'us_equity', 'rule', 'market', NULL),
    ('3',  'Momentum',                   'us_equity', 'rule', 'medium', NULL),
    ('4',  'Mean reversion',             'us_equity', 'rule', 'medium', NULL),
    ('5',  'Trend (MA crossover)',       'us_equity', 'rule', 'medium', NULL),
    ('6',  'Long-short momentum',        'us_equity', 'rule', 'medium', NULL),
    ('7',  'Constrained AI (Claude)',    'us_equity', 'ai',   'medium', NULL),
    ('8',  'Autonomous Claude',          'us_equity', 'ai',   'medium', 2),
    ('9',  'Autonomous GPT',             'us_equity', 'ai',   'medium', NULL),
    ('10', 'Autonomous Gemini',          'us_equity', 'ai',   'medium', NULL),
    ('11', 'Autonomous DeepSeek',        'us_equity', 'ai',   'medium', NULL),
    ('12', 'Autonomous Grok',            'us_equity', 'ai',   'medium', NULL),
    ('13', 'Adaptive blender (Claude)',  'us_equity', 'ai',   'medium', 3),
    ('14', 'AI Cautious (Claude)',       'us_equity', 'ai',   'low',    NULL),
    ('15', 'AI Aggressive (Claude)',     'us_equity', 'ai',   'high',   NULL),
    ('C0', 'Bitcoin benchmark',          'crypto',    'rule', 'crypto', NULL),
    ('C1', 'AI crypto trader (Claude)',  'crypto',    'ai',   'crypto', NULL);

-- ---------------------------------------------------------------------------
-- prices: daily bars, versioned by fetch (point-in-time)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prices (
    price_id     INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL,                     -- 'AAPL', 'BRK.B', 'BTC/USD'
    asset_class  TEXT NOT NULL CHECK (asset_class IN ('us_equity', 'crypto')),
    bar_date     TEXT NOT NULL CHECK (bar_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    bar_ts       TEXT NOT NULL                      -- bar timestamp as given by Alpaca, in UTC
                 CHECK (bar_ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    -- raw (unadjusted) bar
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    -- split-adjusted bar, as known at fetched_at
    adj_open     REAL NOT NULL,
    adj_high     REAL NOT NULL,
    adj_low      REAL NOT NULL,
    adj_close    REAL NOT NULL,
    adj_volume   REAL NOT NULL,
    vwap         REAL,                              -- raw
    trade_count  INTEGER,
    feed         TEXT NOT NULL,                     -- 'iex', 'crypto', ...
    fetched_at   TEXT NOT NULL                      -- one value per fetch batch
                 CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    UNIQUE (symbol, bar_date, fetched_at)
);

-- ---------------------------------------------------------------------------
-- snapshots: one per asset class per day (§4, §13)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id    INTEGER PRIMARY KEY,
    asset_class    TEXT NOT NULL CHECK (asset_class IN ('us_equity', 'crypto')),
    snapshot_date  TEXT NOT NULL CHECK (snapshot_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    -- Knowledge time: only bars with bar_ts <= cutoff_ts AND fetched_at <= cutoff_ts
    -- may be used, and no decision may be timestamped before it.
    cutoff_ts      TEXT NOT NULL
                   CHECK (cutoff_ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    built_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                   CHECK (built_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    payload        TEXT NOT NULL CHECK (json_valid(payload)),
    sha256         TEXT NOT NULL CHECK (length(sha256) = 64),
    checks         TEXT NOT NULL CHECK (json_valid(checks)),   -- data-check results
    checks_passed  INTEGER NOT NULL CHECK (checks_passed IN (0, 1)),
    corrects_id    INTEGER REFERENCES snapshots(snapshot_id),
    CHECK (built_at >= cutoff_ts)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_day
    ON snapshots (asset_class, snapshot_date) WHERE corrects_id IS NULL;

-- ---------------------------------------------------------------------------
-- decisions: one row per agent per decision attempt (§8: every call is logged)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    decision_id       INTEGER PRIMARY KEY,
    agent_id          TEXT NOT NULL REFERENCES agents(agent_id),
    snapshot_id       INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    attempt           INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),  -- retries = 2, 3, ...
    decided_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                      CHECK (decided_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    status            TEXT NOT NULL CHECK (status IN ('ok', 'invalid', 'unavailable', 'capped')),
    -- AI fields (NULL for rule-based agents)
    model_id          TEXT,                         -- e.g. 'anthropic/claude-sonnet-5.5'
    provider_served   TEXT,                         -- provider that actually served the call
    reasoning_setting TEXT,                         -- 'default', 'medium', ...
    temperature       REAL,                         -- NULL = provider default (recorded in raw)
    prompt_hash       TEXT,                         -- sha256 of the exact prompt sent
    raw_response      TEXT,
    tokens_in         INTEGER CHECK (tokens_in >= 0),
    tokens_out        INTEGER CHECK (tokens_out >= 0),
    cost_usd          REAL CHECK (cost_usd >= 0),
    latency_ms        INTEGER CHECK (latency_ms >= 0),
    -- output (all agents)
    parsed_targets    TEXT CHECK (parsed_targets IS NULL OR json_valid(parsed_targets)),  -- {"AAPL": 0.1, ...}
    reasons           TEXT CHECK (reasons IS NULL OR json_valid(reasons)),                -- {"AAPL": "...", ...}
    overall_reasoning TEXT,
    confidence        REAL CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
    error             TEXT,                         -- validation / transport error, if any
    corrects_id       INTEGER REFERENCES decisions(decision_id),
    -- added 17 Nov 2026 (§8 v1.11); last so a fresh table matches one upgraded by
    -- ALTER TABLE ADD COLUMN (db.COLUMN_UPGRADES). Part of tokens_out; NULL = not reported
    reasoning_tokens  INTEGER CHECK (reasoning_tokens IS NULL OR (reasoning_tokens >= 0 AND (tokens_out IS NULL OR reasoning_tokens <= tokens_out))),
    CHECK (status <> 'ok' OR parsed_targets IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_attempt
    ON decisions (agent_id, snapshot_id, attempt) WHERE corrects_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_decisions_agent ON decisions (agent_id, decided_at);

-- ---------------------------------------------------------------------------
-- risk_events: clip, rejection or forced reduction (§6)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_events (
    risk_event_id     INTEGER PRIMARY KEY,
    agent_id          TEXT NOT NULL REFERENCES agents(agent_id),
    decision_id       INTEGER REFERENCES decisions(decision_id),  -- NULL for margin cuts
    occurred_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                      CHECK (occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    event_type        TEXT NOT NULL CHECK (event_type IN ('clip', 'reject', 'forced_reduction')),
    rule              TEXT NOT NULL,                -- e.g. 'max_per_stock', 'easy_to_borrow', 'margin'
    symbol            TEXT,                         -- NULL if portfolio-wide
    requested_weight  REAL,
    applied_weight    REAL,
    detail            TEXT CHECK (detail IS NULL OR json_valid(detail)),
    corrects_id       INTEGER REFERENCES risk_events(risk_event_id)
);

-- ---------------------------------------------------------------------------
-- orders: written once (pending = no row in fills yet)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    order_id          INTEGER PRIMARY KEY,
    agent_id          TEXT NOT NULL REFERENCES agents(agent_id),
    decision_id       INTEGER REFERENCES decisions(decision_id),
    risk_event_id     INTEGER REFERENCES risk_events(risk_event_id),
    origin            TEXT NOT NULL CHECK (origin IN ('decision', 'margin', 'delisting')),
    venue             TEXT NOT NULL CHECK (venue IN ('ledger', 'alpaca_mirror')),
    alpaca_account    INTEGER CHECK (alpaca_account IN (1, 2, 3)),
    alpaca_order_id   TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                      CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    symbol            TEXT NOT NULL,
    asset_class       TEXT NOT NULL CHECK (asset_class IN ('us_equity', 'crypto')),
    side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty               REAL CHECK (qty > 0),
    notional_usd      REAL CHECK (notional_usd > 0),
    target_weight     REAL,
    corrects_id       INTEGER REFERENCES orders(order_id),
    CHECK (qty IS NOT NULL OR notional_usd IS NOT NULL),
    CHECK (origin <> 'decision' OR decision_id IS NOT NULL),
    CHECK (origin <> 'margin' OR risk_event_id IS NOT NULL),
    CHECK (venue <> 'alpaca_mirror' OR alpaca_account IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_orders_agent ON orders (agent_id, created_at);

-- ---------------------------------------------------------------------------
-- fills: the fill (or cancellation) of an order (§7)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    fill_id           INTEGER PRIMARY KEY,
    order_id          INTEGER NOT NULL REFERENCES orders(order_id),
    status            TEXT NOT NULL CHECK (status IN ('filled', 'cancelled')),
    filled_at         TEXT NOT NULL
                      CHECK (filled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    qty               REAL CHECK (qty > 0),
    reference_price   REAL CHECK (reference_price > 0),  -- official open (equity) / next price (crypto)
    slippage_bps      REAL,
    fill_price        REAL CHECK (fill_price > 0),
    fee_usd           REAL NOT NULL DEFAULT 0 CHECK (fee_usd >= 0),
    detail            TEXT CHECK (detail IS NULL OR json_valid(detail)),  -- e.g. cancel reason
    recorded_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                      CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    corrects_id       INTEGER REFERENCES fills(fill_id),
    CHECK (status <> 'filled' OR (qty IS NOT NULL AND fill_price IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_fills_order ON fills (order_id);

-- ---------------------------------------------------------------------------
-- positions: agent x day x symbol (end-of-day)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    position_id     INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(agent_id),
    as_of_date      TEXT NOT NULL CHECK (as_of_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL CHECK (asset_class IN ('us_equity', 'crypto')),
    qty             REAL NOT NULL,                  -- negative = short
    avg_cost        REAL,
    mark_price      REAL NOT NULL,
    market_value    REAL NOT NULL,
    unrealised_pnl  REAL,
    recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    corrects_id     INTEGER REFERENCES positions(position_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_day
    ON positions (agent_id, as_of_date, symbol) WHERE corrects_id IS NULL;

-- ---------------------------------------------------------------------------
-- equity: agent x day
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS equity (
    equity_id          INTEGER PRIMARY KEY,
    agent_id           TEXT NOT NULL REFERENCES agents(agent_id),
    as_of_date         TEXT NOT NULL CHECK (as_of_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    cash               REAL NOT NULL,
    long_value         REAL NOT NULL DEFAULT 0,
    short_value        REAL NOT NULL DEFAULT 0,    -- reported as a positive number
    equity             REAL NOT NULL,
    gross_exposure     REAL NOT NULL DEFAULT 0,
    borrow_fee_usd     REAL NOT NULL DEFAULT 0,    -- charged this day
    margin_interest_usd REAL NOT NULL DEFAULT 0,   -- charged this day
    trading_fees_usd   REAL NOT NULL DEFAULT 0,    -- crypto 0.25% fees this day
    recorded_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                       CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    corrects_id        INTEGER REFERENCES equity(equity_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_equity_day
    ON equity (agent_id, as_of_date) WHERE corrects_id IS NULL;

-- ---------------------------------------------------------------------------
-- mirror_recon: ledger vs Alpaca paper account, per mirrored agent per day (D1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_recon (
    recon_id         INTEGER PRIMARY KEY,
    agent_id         TEXT NOT NULL REFERENCES agents(agent_id),
    as_of_date       TEXT NOT NULL CHECK (as_of_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    alpaca_account   INTEGER NOT NULL CHECK (alpaca_account IN (1, 2, 3)),
    ledger_equity    REAL NOT NULL,
    alpaca_equity    REAL NOT NULL,
    difference_usd   REAL NOT NULL,                -- alpaca - ledger
    detail           TEXT CHECK (detail IS NULL OR json_valid(detail)),  -- per-position diffs, explanation
    recorded_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                     CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    corrects_id      INTEGER REFERENCES mirror_recon(recon_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_mirror_recon_day
    ON mirror_recon (agent_id, as_of_date) WHERE corrects_id IS NULL;

-- ---------------------------------------------------------------------------
-- heartbeat: one row per run event (append-only, so start and finish are rows)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heartbeat (
    heartbeat_id  INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL,                    -- shared by a run's events
    job           TEXT NOT NULL,                    -- 'fetch_prices', 'build_snapshot', 'decide', 'fill', ...
    event         TEXT NOT NULL CHECK (event IN ('started', 'finished', 'error')),
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                  CHECK (at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    detail        TEXT,
    corrects_id   INTEGER REFERENCES heartbeat(heartbeat_id)
);
CREATE INDEX IF NOT EXISTS ix_heartbeat_run ON heartbeat (run_id);

-- ---------------------------------------------------------------------------
-- market calendar: NYSE sessions from Alpaca, cached as versioned fetches
-- ---------------------------------------------------------------------------
-- Alpaca lists trading days only, so each fetch records the range it asked
-- about. For a date D the answer comes from the newest fetch whose range
-- contains D: a session row means open (with that close), no row means closed.
-- A closure announced later simply appears as a newer fetch without that day.
CREATE TABLE IF NOT EXISTS calendar_fetches (
    fetch_id     INTEGER PRIMARY KEY,
    range_start  TEXT NOT NULL CHECK (range_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    range_end    TEXT NOT NULL CHECK (range_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    fetched_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                 CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    n_sessions   INTEGER NOT NULL CHECK (n_sessions >= 0),
    CHECK (range_end >= range_start)
);
CREATE INDEX IF NOT EXISTS ix_calendar_fetches_range ON calendar_fetches (range_start, range_end);

CREATE TABLE IF NOT EXISTS market_sessions (
    session_id    INTEGER PRIMARY KEY,
    fetch_id      INTEGER NOT NULL REFERENCES calendar_fetches(fetch_id),
    session_date  TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    open_ts       TEXT NOT NULL CHECK (open_ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),   -- UTC (Alpaca gives ET)
    close_ts      TEXT NOT NULL CHECK (close_ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),  -- UTC; early closes included
    CHECK (close_ts > open_ts),
    UNIQUE (fetch_id, session_date)
);

-- ---------------------------------------------------------------------------
-- crypto_fill_bars: closed 1-minute bars after each crypto decision (§7 rule 4, v1.12)
-- ---------------------------------------------------------------------------
-- The crypto fill price source: the closed minute bars in [window_start,
-- window_start + 60 min), window_start = 00:15Z after the daily bar of
-- decision_date closes. Versioned like prices (a re-fetch inserts a new
-- version only when a bar differs). No snapshot ever reads this table.
CREATE TABLE IF NOT EXISTS crypto_fill_bars (
    fill_bar_id    INTEGER PRIMARY KEY,
    symbol         TEXT NOT NULL,                   -- 'BTC/USD'
    decision_date  TEXT NOT NULL CHECK (decision_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    window_start   TEXT NOT NULL CHECK (window_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    bar_ts         TEXT NOT NULL CHECK (bar_ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),   -- minute start, UTC
    open           REAL NOT NULL CHECK (open > 0),
    high           REAL NOT NULL CHECK (high > 0),
    low            REAL NOT NULL CHECK (low > 0),
    close          REAL NOT NULL CHECK (close > 0),
    volume         REAL NOT NULL CHECK (volume >= 0),
    vwap           REAL,
    trade_count    INTEGER,
    feed           TEXT NOT NULL,
    fetched_at     TEXT NOT NULL CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    CHECK (bar_ts >= window_start),
    CHECK (bar_ts < strftime('%Y-%m-%dT%H:%M:%fZ', window_start, '+60 minutes')),
    CHECK (fetched_at >= strftime('%Y-%m-%dT%H:%M:%fZ', bar_ts, '+1 minute')),   -- closed when fetched
    UNIQUE (symbol, bar_ts, fetched_at)
);
CREATE INDEX IF NOT EXISTS ix_crypto_fill_bars ON crypto_fill_bars (symbol, bar_ts);

-- ---------------------------------------------------------------------------
-- crypto_fill_windows: a fill window fetched in full after it closed (§7 rule 4, v1.13)
-- ---------------------------------------------------------------------------
-- One row per fetch of a whole window [window_start, window_start + 60 min),
-- written by data/fill_prices.py only once every minute of it has closed.
-- It is the ledger's evidence that a coin with no bar in the window really had
-- none (no trades), rather than not yet fetched: only then is its order
-- cancelled. window_start is the day's crypto decision time (t_ref).
CREATE TABLE IF NOT EXISTS crypto_fill_windows (
    window_id      INTEGER PRIMARY KEY,
    decision_date  TEXT NOT NULL CHECK (decision_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    window_start   TEXT NOT NULL CHECK (window_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:00.000Z'),
    window_end     TEXT NOT NULL CHECK (window_end = strftime('%Y-%m-%dT%H:%M:%fZ', window_start, '+60 minutes')),
    symbols        TEXT NOT NULL CHECK (json_valid(symbols)),   -- the coins asked for, JSON list
    fetched_at     TEXT NOT NULL CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    run_id         TEXT,
    CHECK (fetched_at >= window_end)                            -- the whole window had closed
);
CREATE INDEX IF NOT EXISTS ix_crypto_fill_windows ON crypto_fill_windows (decision_date, window_start);

-- ---------------------------------------------------------------------------
-- experiment: the label of this database file (protocol §9, v1.13)
-- ---------------------------------------------------------------------------
-- One database file per experiment (shakedown, official, or a dev copy). The
-- single row names it and its first decision day; the daily runner refuses a
-- file without it, or a date before start_date. Written by runner/experiment.py.
CREATE TABLE IF NOT EXISTS experiment (
    experiment_id  INTEGER PRIMARY KEY,
    name           TEXT NOT NULL CHECK (name GLOB '[a-z]*' AND name NOT GLOB '*[^a-z0-9_-]*'),
    start_date     TEXT NOT NULL CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    source         TEXT,                            -- where the market data was copied from
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                   CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z')
);

-- ---------------------------------------------------------------------------
-- Integrity triggers
-- ---------------------------------------------------------------------------

-- A database file holds one experiment.
CREATE TRIGGER IF NOT EXISTS experiment_single_row
BEFORE INSERT ON experiment
WHEN (SELECT COUNT(*) FROM experiment) > 0
BEGIN
    SELECT RAISE(ABORT, 'this database already has its experiment label');
END;

-- No future data (CLAUDE.md rule 1): a decision can't predate its snapshot's cutoff.
CREATE TRIGGER IF NOT EXISTS decisions_not_before_cutoff
BEFORE INSERT ON decisions
WHEN NEW.decided_at < (SELECT cutoff_ts FROM snapshots WHERE snapshot_id = NEW.snapshot_id)
BEGIN
    SELECT RAISE(ABORT, 'decision timestamped before its snapshot cutoff (CLAUDE.md rule 1)');
END;

-- An agent only decides on a snapshot of its own asset class.
CREATE TRIGGER IF NOT EXISTS decisions_asset_class_match
BEFORE INSERT ON decisions
WHEN (SELECT asset_class FROM agents WHERE agent_id = NEW.agent_id)
  <> (SELECT asset_class FROM snapshots WHERE snapshot_id = NEW.snapshot_id)
BEGIN
    SELECT RAISE(ABORT, 'agent and snapshot asset classes differ');
END;

-- Append-only (CLAUDE.md rule 2) on every data table.
CREATE TRIGGER IF NOT EXISTS prices_no_update       BEFORE UPDATE ON prices       BEGIN SELECT RAISE(ABORT, 'prices is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS prices_no_delete       BEFORE DELETE ON prices       BEGIN SELECT RAISE(ABORT, 'prices is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update    BEFORE UPDATE ON snapshots    BEGIN SELECT RAISE(ABORT, 'snapshots is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete    BEFORE DELETE ON snapshots    BEGIN SELECT RAISE(ABORT, 'snapshots is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update    BEFORE UPDATE ON decisions    BEGIN SELECT RAISE(ABORT, 'decisions is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete    BEFORE DELETE ON decisions    BEGIN SELECT RAISE(ABORT, 'decisions is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS risk_events_no_update  BEFORE UPDATE ON risk_events  BEGIN SELECT RAISE(ABORT, 'risk_events is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS risk_events_no_delete  BEFORE DELETE ON risk_events  BEGIN SELECT RAISE(ABORT, 'risk_events is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_update       BEFORE UPDATE ON orders       BEGIN SELECT RAISE(ABORT, 'orders is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_delete       BEFORE DELETE ON orders       BEGIN SELECT RAISE(ABORT, 'orders is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_update        BEFORE UPDATE ON fills        BEGIN SELECT RAISE(ABORT, 'fills is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete        BEFORE DELETE ON fills        BEGIN SELECT RAISE(ABORT, 'fills is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS positions_no_update    BEFORE UPDATE ON positions    BEGIN SELECT RAISE(ABORT, 'positions is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS positions_no_delete    BEFORE DELETE ON positions    BEGIN SELECT RAISE(ABORT, 'positions is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS equity_no_update       BEFORE UPDATE ON equity       BEGIN SELECT RAISE(ABORT, 'equity is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS equity_no_delete       BEFORE DELETE ON equity       BEGIN SELECT RAISE(ABORT, 'equity is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS mirror_recon_no_update BEFORE UPDATE ON mirror_recon BEGIN SELECT RAISE(ABORT, 'mirror_recon is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS mirror_recon_no_delete BEFORE DELETE ON mirror_recon BEGIN SELECT RAISE(ABORT, 'mirror_recon is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS heartbeat_no_update    BEFORE UPDATE ON heartbeat    BEGIN SELECT RAISE(ABORT, 'heartbeat is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS heartbeat_no_delete    BEFORE DELETE ON heartbeat    BEGIN SELECT RAISE(ABORT, 'heartbeat is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS calendar_fetches_no_update BEFORE UPDATE ON calendar_fetches BEGIN SELECT RAISE(ABORT, 'calendar_fetches is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS calendar_fetches_no_delete BEFORE DELETE ON calendar_fetches BEGIN SELECT RAISE(ABORT, 'calendar_fetches is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS market_sessions_no_update  BEFORE UPDATE ON market_sessions  BEGIN SELECT RAISE(ABORT, 'market_sessions is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS market_sessions_no_delete  BEFORE DELETE ON market_sessions  BEGIN SELECT RAISE(ABORT, 'market_sessions is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS crypto_fill_bars_no_update BEFORE UPDATE ON crypto_fill_bars BEGIN SELECT RAISE(ABORT, 'crypto_fill_bars is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS crypto_fill_bars_no_delete BEFORE DELETE ON crypto_fill_bars BEGIN SELECT RAISE(ABORT, 'crypto_fill_bars is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS crypto_fill_windows_no_update BEFORE UPDATE ON crypto_fill_windows BEGIN SELECT RAISE(ABORT, 'crypto_fill_windows is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS crypto_fill_windows_no_delete BEFORE DELETE ON crypto_fill_windows BEGIN SELECT RAISE(ABORT, 'crypto_fill_windows is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS experiment_no_update       BEFORE UPDATE ON experiment       BEGIN SELECT RAISE(ABORT, 'experiment is append-only (CLAUDE.md rule 2)'); END;
CREATE TRIGGER IF NOT EXISTS experiment_no_delete       BEFORE DELETE ON experiment       BEGIN SELECT RAISE(ABORT, 'experiment is append-only (CLAUDE.md rule 2)'); END;
