import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.paper_evidence import audit_paper_evidence
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

NOW = datetime(2026, 7, 29, 4, tzinfo=UTC)


def setup_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    return database


def insert_session(
    database: Database,
    tmp_path: Path,
    *,
    kind: str = "dry",
    clean: bool = True,
    with_bar: bool = True,
) -> str:
    session_id = str(uuid4())
    report = tmp_path / f"{session_id}.json"
    report.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "clean_shutdown": clean,
                "reconciliation_healthy": clean,
            }
        ),
        encoding="utf-8",
    )
    with database.transaction(write=True) as connection:
        connection.execute(
            """
            INSERT INTO paper_runtime_sessions (
                session_id, evidence_kind, state, started_at, ended_at,
                clean_shutdown, reconciliation_healthy, git_commit,
                release_manifest_hash, strategy_manifest_hash, config_hash, report_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                kind,
                "STOPPED" if clean else "FAILED",
                NOW.isoformat(),
                NOW.isoformat(),
                int(clean),
                int(clean),
                "a" * 40,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                str(report),
            ),
        )
        kinds = ["preflight", "shutdown"] + (["bar"] if with_bar else [])
        for snapshot_kind in kinds:
            connection.execute(
                "INSERT INTO runtime_snapshots VALUES (?, ?, ?, '{}', ?)",
                (str(uuid4()), session_id, snapshot_kind, NOW.isoformat()),
            )
    return session_id


def test_empty_audit_is_pending_and_read_only(tmp_path: Path) -> None:
    database = setup_database(tmp_path)
    before = database.path.stat().st_size
    audit = audit_paper_evidence(database)

    assert (audit.successful_dry_sessions, audit.successful_formal_sessions) == (0, 0)
    assert audit.operational_acceptance_met is False
    assert "10 audited dry sessions remain" in audit.blockers
    assert database.path.stat().st_size == before


def test_audit_counts_only_complete_unique_sequenced_evidence(tmp_path: Path) -> None:
    database = setup_database(tmp_path)
    valid = insert_session(database, tmp_path)
    duplicate = insert_session(database, tmp_path)
    incomplete = insert_session(database, tmp_path, with_bar=False)
    formal = insert_session(database, tmp_path, kind="formal")

    audit = audit_paper_evidence(database)

    assert audit.successful_dry_sessions == 1
    assert audit.successful_formal_sessions == 0
    codes = {(issue.session_id, issue.code) for issue in audit.issues}
    assert any(
        session_id in {valid, duplicate} and code == "duplicate_session_date"
        for session_id, code in codes
    )
    assert (incomplete, "session_snapshots_missing") in codes
    assert (formal, "formal_before_dry_gate") in codes
    assert (
        sum(
            issue.code == "duplicate_session_date" and issue.session_id in {valid, duplicate}
            for issue in audit.issues
        )
        == 1
    )


def test_failed_or_mismatched_reports_never_count(tmp_path: Path) -> None:
    database = setup_database(tmp_path)
    failed = insert_session(database, tmp_path, clean=False)
    mismatch = insert_session(database, tmp_path)
    connection = database.connect(read_only=True)
    try:
        report_path = Path(
            connection.execute(
                "SELECT report_path FROM paper_runtime_sessions WHERE session_id=?", (mismatch,)
            ).fetchone()[0]
        )
    finally:
        connection.close()
    report_path.write_text(
        json.dumps({"session_id": "wrong", "clean_shutdown": True, "reconciliation_healthy": True}),
        encoding="utf-8",
    )

    audit = audit_paper_evidence(database)
    assert audit.successful_dry_sessions == 0
    assert {issue.session_id for issue in audit.issues} == {failed, mismatch}


def test_status_cli_reports_pending_without_mutation(tmp_path: Path) -> None:
    database = setup_database(tmp_path)
    result = CliRunner().invoke(app, ["paper-evidence-status", "--path", str(database.path)])

    assert result.exit_code == 0
    assert "Audited dry sessions: 0/10" in result.stdout
    assert "Audited formal sessions: 0/30" in result.stdout
    assert "operational acceptance: PENDING" in result.stdout
