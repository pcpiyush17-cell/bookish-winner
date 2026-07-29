CREATE TABLE paper_runtime_sessions (
    session_id TEXT PRIMARY KEY,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('dry', 'formal')),
    state TEXT NOT NULL CHECK (state IN (
        'CREATED', 'PREFLIGHT', 'READY', 'RUNNING', 'STOPPING', 'STOPPED',
        'FAILED', 'INTERRUPTED'
    )),
    started_at TEXT NOT NULL,
    ready_at TEXT,
    ended_at TEXT,
    clean_shutdown INTEGER NOT NULL DEFAULT 0 CHECK (clean_shutdown IN (0, 1)),
    reconciliation_healthy INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_healthy IN (0, 1)),
    git_commit TEXT NOT NULL,
    release_manifest_hash TEXT NOT NULL CHECK (length(release_manifest_hash) = 64),
    strategy_manifest_hash TEXT NOT NULL CHECK (length(strategy_manifest_hash) = 64),
    config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
    report_path TEXT,
    failure_code TEXT
) STRICT;

CREATE TABLE runtime_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES paper_runtime_sessions(session_id),
    snapshot_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX paper_runtime_sessions_state_idx ON paper_runtime_sessions(state, started_at);
CREATE INDEX runtime_snapshots_session_idx ON runtime_snapshots(session_id, created_at);
