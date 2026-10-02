-- GENERADO, no editar a mano:
--   python -m database.models > database/schema.sql
-- Fuente de verdad: database/models.py (SCHEMA, INDEXES, MIGRATIONS).
-- `tests/unit/test_schema.py::test_schema_sql_al_dia` falla si se separan.

CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        system_prompt TEXT NOT NULL,
        allowed_tools TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'auto',
        model TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        role_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        volume REAL,
        price_open REAL,
        price_close REAL,
        profit REAL,
        time_close TEXT,
        synced_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT,
        ticket TEXT,
        symbol TEXT NOT NULL DEFAULT 'EURUSD',
        action TEXT,
        poi_type TEXT,
        liquidity_swept TEXT,
        cme_confirmation TEXT,
        setup_json TEXT NOT NULL DEFAULT '{}',
        emotion TEXT,
        plan_compliance INTEGER,
        tags TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        timestamp TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS agent_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS chart_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        price REAL NOT NULL,
        label TEXT,
        side TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        triggered_at TEXT
    );

CREATE TABLE IF NOT EXISTS cot_reports (
        report_date TEXT PRIMARY KEY,
        am_net INTEGER,
        lf_net INTEGER,
        nc_net INTEGER,
        cot_index_26w REAL,
        macro_bias TEXT,
        delta_am REAL,
        delta_lf REAL,
        created_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS setup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT NOT NULL,
        verdict TEXT NOT NULL,
        score REAL NOT NULL,
        breakdown_json TEXT NOT NULL DEFAULT '{}',
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        validated INTEGER,
        reject_reasons TEXT NOT NULL DEFAULT '[]',
        risk_state_json TEXT NOT NULL DEFAULT '{}',
        trade_result_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        snapshot_age_s REAL,
        source TEXT NOT NULL DEFAULT '',
        timestamp TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS trade_excursions (
        ticket INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL DEFAULT '',
        price_open REAL,
        best_price REAL,
        worst_price REAL,
        samples INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT,
        updated_at TEXT
    );

CREATE TABLE IF NOT EXISTS setup_state (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT,
        score REAL,
        verdict TEXT,
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        status TEXT NOT NULL DEFAULT 'active',
        notified INTEGER NOT NULL DEFAULT 0,
        auto_executed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    );

CREATE TABLE IF NOT EXISTS watcher_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS prop_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS chart_drawings (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        drawings_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    );

-- Índices
CREATE INDEX IF NOT EXISTS idx_setup_log_time ON setup_log(timestamp);

CREATE INDEX IF NOT EXISTS idx_setup_log_verdict ON setup_log(verdict);

CREATE INDEX IF NOT EXISTS idx_setup_log_age ON setup_log(snapshot_age_s);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time_close);

CREATE INDEX IF NOT EXISTS idx_journal_time ON journal(timestamp);

-- Migraciones idempotentes (ALTER TABLE ... ADD COLUMN)
-- Se aplican en init_schema(); aquí solo como referencia.
-- trades         ADD COLUMN time_open      REAL
-- trades         ADD COLUMN commission     REAL
-- trades         ADD COLUMN swap           REAL
-- trades         ADD COLUMN exit_reason    INTEGER
-- roles          ADD COLUMN active         INTEGER NOT NULL DEFAULT 1
-- messages       ADD COLUMN tool_call_id   TEXT
-- messages       ADD COLUMN name           TEXT
-- chart_alerts   ADD COLUMN conditions     TEXT NOT NULL DEFAULT '[]'
-- chart_alerts   ADD COLUMN timeframe      TEXT
-- chart_alerts   ADD COLUMN expires_at     TEXT
-- chart_alerts   ADD COLUMN last_check     TEXT
-- journal        ADD COLUMN entry_price    REAL
-- journal        ADD COLUMN sl_price       REAL
-- journal        ADD COLUMN tp_price       REAL
-- journal        ADD COLUMN time_open      TEXT
-- journal        ADD COLUMN time_close     TEXT
-- setup_log      ADD COLUMN source         TEXT NOT NULL DEFAULT ''
-- setup_log      ADD COLUMN context_json   TEXT NOT NULL DEFAULT '{}'
-- setup_log      ADD COLUMN snapshot_age_s REAL
