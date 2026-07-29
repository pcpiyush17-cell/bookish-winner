CREATE TABLE shadow_reports (
    report_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    broker_user_id TEXT NOT NULL,
    intended_count INTEGER NOT NULL CHECK (intended_count >= 0),
    difference_count INTEGER NOT NULL CHECK (difference_count >= 0),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    difference_json TEXT NOT NULL CHECK (json_valid(difference_json)),
    report_path TEXT NOT NULL,
    report_sha256 TEXT NOT NULL CHECK (length(report_sha256) = 64)
) STRICT;

CREATE INDEX shadow_reports_captured_idx ON shadow_reports(captured_at);
