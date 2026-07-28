CREATE TABLE risk_decisions (
    decision_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'resized', 'rejected')),
    requested_quantity INTEGER NOT NULL,
    approved_quantity INTEGER NOT NULL CHECK (approved_quantity >= 0),
    reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
    config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    evaluated_at TEXT NOT NULL
) STRICT;

CREATE INDEX risk_decisions_idempotency_time_idx
ON risk_decisions(idempotency_key, evaluated_at);

CREATE TABLE kill_switch_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    reason TEXT NOT NULL,
    activated_at TEXT,
    reset_at TEXT,
    CHECK (
        (active = 1 AND activated_at IS NOT NULL AND reset_at IS NULL)
        OR (active = 0)
    )
) STRICT;

INSERT INTO kill_switch_state(singleton, active, reason) VALUES (1, 0, '');

CREATE TABLE circuit_breaker_events (
    event_id TEXT PRIMARY KEY,
    trigger_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    occurred_at TEXT NOT NULL
) STRICT;
