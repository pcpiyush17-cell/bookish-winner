CREATE TABLE control_audit (
    audit_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    action TEXT NOT NULL CHECK (action IN (
        'kill_switch_activate', 'kill_switch_reset', 'runtime_stop'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN ('denied', 'failed', 'succeeded')),
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
) STRICT;

CREATE INDEX control_audit_time_idx ON control_audit(occurred_at);
