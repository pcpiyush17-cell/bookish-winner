CREATE TABLE runtime_sessions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK (mode IN ('backtest', 'replay', 'paper', 'shadow', 'live')),
    config_hash TEXT NOT NULL CHECK (
        length(config_hash) = 64 AND config_hash NOT GLOB '*[^0-9a-f]*'
    ),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'stopped', 'interrupted')),
    CHECK (
        (status = 'running' AND ended_at IS NULL)
        OR (status != 'running' AND ended_at IS NOT NULL)
    ),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
) STRICT;

CREATE TABLE event_log (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
    correlation_id TEXT,
    causation_id TEXT,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_hash TEXT NOT NULL CHECK (
        length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'
    ),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX event_log_run_id_idx ON event_log(run_id);
CREATE INDEX event_log_type_occurred_idx ON event_log(event_type, occurred_at);
