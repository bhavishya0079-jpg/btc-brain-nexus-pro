CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- t is the bar-open time in epoch milliseconds
CREATE TABLE candles (
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL,
    t INTEGER NOT NULL,
    o REAL NOT NULL,
    h REAL NOT NULL,
    l REAL NOT NULL,
    c REAL NOT NULL,
    v REAL NOT NULL,
    PRIMARY KEY (provider, symbol, tf, t)
);

CREATE TABLE strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    active_version INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active | candidate | archived
    parent_version INTEGER,
    note TEXT,
    created_by TEXT NOT NULL DEFAULT 'user',    -- user | ai | restore
    created_at REAL NOT NULL,
    UNIQUE (strategy_id, version)
);

CREATE TABLE backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    strategy_id INTEGER,
    version INTEGER,
    provider TEXT,
    symbol TEXT,
    tf TEXT,
    params TEXT NOT NULL,
    stats TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE paper_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    fee_pct REAL NOT NULL DEFAULT 0.05,
    strategy_id INTEGER,
    strategy_version INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side INTEGER NOT NULL,                      -- 1 long, -1 short
    qty REAL NOT NULL,
    remaining REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    targets TEXT NOT NULL DEFAULT '[]',
    opened_at REAL NOT NULL,
    closed_at REAL,
    status TEXT NOT NULL DEFAULT 'open',
    last_price REAL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    exits TEXT NOT NULL DEFAULT '[]',
    note TEXT
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT,
    symbol TEXT,
    tf TEXT,
    params TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_triggered_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    alert_id INTEGER,
    message TEXT NOT NULL,
    data TEXT,
    created_at REAL NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    server_notified INTEGER NOT NULL DEFAULT 0
);

-- AI learning workflow: suggested -> validated | rejected -> approved -> applied (-> rolled_back)
CREATE TABLE learning_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    strategy_id INTEGER NOT NULL,
    base_version INTEGER NOT NULL,
    candidate_version INTEGER,
    status TEXT NOT NULL,
    note TEXT,
    verdict TEXT,
    detail TEXT NOT NULL,
    data_ref TEXT,
    paper_account_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE provider_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX idx_paper_positions_account ON paper_positions(account_id, status);
CREATE INDEX idx_alert_events_seen ON alert_events(user_id, seen, id);
CREATE INDEX idx_audit_created ON audit_log(created_at);
CREATE INDEX idx_versions_strategy ON strategy_versions(strategy_id, version);
