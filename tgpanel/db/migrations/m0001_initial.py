"""Initial schema (PLAN section 4)."""

SQL = """
CREATE TABLE pools (
    id INTEGER PRIMARY KEY,
    port INTEGER NOT NULL UNIQUE,
    stats_port INTEGER NOT NULL UNIQUE,
    managed INTEGER NOT NULL DEFAULT 1 CHECK (managed IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    comment TEXT NOT NULL DEFAULT '',
    tg_id INTEGER UNIQUE,
    tg_username TEXT,
    secret TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'expired')),
    disabled_reason TEXT,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    loopback_ip TEXT NOT NULL UNIQUE,
    carrier_mode TEXT CHECK (carrier_mode IN
        ('https', 'https-lanes', 'websocket', 'websocket-lanes')),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    can_message INTEGER NOT NULL DEFAULT 0 CHECK (can_message IN (0, 1)),
    bot_started INTEGER NOT NULL DEFAULT 0 CHECK (bot_started IN (0, 1)),
    imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0, 1)),
    source_profile_name TEXT
);
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_pool ON users(pool_id);
CREATE INDEX idx_users_expires ON users(expires_at);
CREATE INDEX idx_users_created ON users(created_at);
CREATE INDEX idx_users_last_seen ON users(last_seen_at);
CREATE INDEX idx_users_tg_username ON users(tg_username);

CREATE TABLE traffic_minute (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket_ts INTEGER NOT NULL,
    bytes_up INTEGER NOT NULL DEFAULT 0,
    bytes_down INTEGER NOT NULL DEFAULT 0,
    packets_up INTEGER NOT NULL DEFAULT 0,
    packets_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket_ts)
) WITHOUT ROWID;
CREATE INDEX idx_traffic_minute_ts ON traffic_minute(bucket_ts);

CREATE TABLE traffic_hour (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket_ts INTEGER NOT NULL,
    bytes_up INTEGER NOT NULL DEFAULT 0,
    bytes_down INTEGER NOT NULL DEFAULT 0,
    packets_up INTEGER NOT NULL DEFAULT 0,
    packets_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket_ts)
) WITHOUT ROWID;
CREATE INDEX idx_traffic_hour_ts ON traffic_hour(bucket_ts);

CREATE TABLE traffic_day (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket_ts INTEGER NOT NULL,
    bytes_up INTEGER NOT NULL DEFAULT 0,
    bytes_down INTEGER NOT NULL DEFAULT 0,
    packets_up INTEGER NOT NULL DEFAULT 0,
    packets_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket_ts)
) WITHOUT ROWID;
CREATE INDEX idx_traffic_day_ts ON traffic_day(bucket_ts);

CREATE TABLE counter_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_up INTEGER NOT NULL DEFAULT 0,
    last_down INTEGER NOT NULL DEFAULT 0,
    last_packets_up INTEGER NOT NULL DEFAULT 0,
    last_packets_down INTEGER NOT NULL DEFAULT 0,
    active_last INTEGER NOT NULL DEFAULT 0 CHECK (active_last IN (0, 1)),
    active_prev INTEGER NOT NULL DEFAULT 0 CHECK (active_prev IN (0, 1)),
    updated_at TEXT
);

CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    tg_username TEXT,
    full_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT
);
CREATE INDEX idx_access_requests_status ON access_requests(status, created_at);
CREATE UNIQUE INDEX idx_access_requests_one_pending
    ON access_requests(tg_id) WHERE status = 'pending';

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE admins (
    tg_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE apply_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    backup_path TEXT,
    error TEXT
);
CREATE INDEX idx_apply_runs_started ON apply_runs(started_at);

CREATE TABLE backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_backups_created ON backups(created_at);

CREATE TABLE broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    template TEXT NOT NULL
);

CREATE TABLE broadcast_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    user_id INTEGER,
    tg_id INTEGER,
    result TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT
);
CREATE INDEX idx_broadcast_items_broadcast ON broadcast_items(broadcast_id);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
CREATE INDEX idx_audit_target ON audit_log(target, ts);
"""
