ALTER TABLE paper_runtime_sessions
ADD COLUMN evidence_source TEXT NOT NULL DEFAULT 'live'
CHECK (evidence_source IN ('live', 'replay'));

CREATE TABLE historical_paper_sessions (
    session_id TEXT PRIMARY KEY REFERENCES paper_runtime_sessions(session_id),
    market_date TEXT NOT NULL UNIQUE,
    instrument_key TEXT NOT NULL,
    interval TEXT NOT NULL CHECK (interval IN ('minute', '15minute')),
    source_manifest_path TEXT NOT NULL,
    source_checksum_sha256 TEXT NOT NULL CHECK (length(source_checksum_sha256) = 64),
    source_rows INTEGER NOT NULL CHECK (source_rows > 0),
    deterministic INTEGER NOT NULL CHECK (deterministic = 1),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX historical_paper_sessions_date_idx
ON historical_paper_sessions(market_date);
