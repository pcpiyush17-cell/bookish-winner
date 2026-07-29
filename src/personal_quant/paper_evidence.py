"""Read-only audit of WP-14 operational paper-session evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from personal_quant.storage.database import Database

_INDIA = ZoneInfo("Asia/Kolkata")
DRY_REQUIRED = 10
FORMAL_REQUIRED = 30


@dataclass(frozen=True, slots=True)
class EvidenceIssue:
    session_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class EvidenceAudit:
    successful_dry_sessions: int
    successful_formal_sessions: int
    dry_requirement_met: bool
    formal_requirement_met: bool
    operational_acceptance_met: bool
    issues: tuple[EvidenceIssue, ...]
    blockers: tuple[str, ...]


def audit_paper_evidence(database: Database) -> EvidenceAudit:
    """Validate persisted evidence without creating, repairing, or promoting any session."""
    connection = database.connect(read_only=True)
    try:
        sessions = connection.execute(
            "SELECT * FROM paper_runtime_sessions ORDER BY started_at, session_id"
        ).fetchall()
        snapshots = connection.execute(
            "SELECT session_id, snapshot_kind FROM runtime_snapshots"
        ).fetchall()
    finally:
        connection.close()
    snapshot_kinds: dict[str, set[str]] = {}
    for row in snapshots:
        snapshot_kinds.setdefault(str(row["session_id"]), set()).add(str(row["snapshot_kind"]))

    issues: list[EvidenceIssue] = []
    counted_dates: set[tuple[str, str]] = set()
    dry = 0
    formal = 0
    for row in sessions:
        session_id = str(row["session_id"])
        kind = str(row["evidence_kind"])
        session_issues = _session_issues(dict(row), snapshot_kinds.get(session_id, set()))
        if kind == "formal" and dry < DRY_REQUIRED:
            session_issues.append(
                EvidenceIssue(
                    session_id,
                    "formal_before_dry_gate",
                    "Formal evidence appeared before ten audited dry sessions",
                )
            )
        try:
            session_date = datetime.fromisoformat(str(row["started_at"])).astimezone(_INDIA).date()
        except ValueError:
            session_date = None
        date_key = (kind, str(session_date))
        if session_date is not None and date_key in counted_dates:
            session_issues.append(
                EvidenceIssue(
                    session_id,
                    "duplicate_session_date",
                    "Only one evidence session of each kind may count per market date",
                )
            )
        issues.extend(session_issues)
        if session_issues:
            continue
        counted_dates.add(date_key)
        if kind == "dry":
            dry += 1
        elif kind == "formal":
            formal += 1

    blockers = _workflow_blockers(dry, formal)
    return EvidenceAudit(
        dry,
        formal,
        dry >= DRY_REQUIRED,
        formal >= FORMAL_REQUIRED,
        dry >= DRY_REQUIRED and formal >= FORMAL_REQUIRED and not issues,
        tuple(issues),
        blockers,
    )


def _session_issues(row: dict[str, object], snapshots: set[str]) -> list[EvidenceIssue]:
    session_id = str(row["session_id"])
    issues: list[EvidenceIssue] = []
    required_values = {
        "state": "STOPPED",
        "clean_shutdown": 1,
        "reconciliation_healthy": 1,
    }
    for field, expected in required_values.items():
        if row[field] != expected:
            issues.append(
                EvidenceIssue(
                    session_id,
                    f"session_{field}_invalid",
                    f"Session {field} is not {expected!r}",
                )
            )
    missing_snapshots = {"preflight", "bar", "shutdown"} - snapshots
    if missing_snapshots:
        issues.append(
            EvidenceIssue(
                session_id,
                "session_snapshots_missing",
                f"Missing snapshots: {', '.join(sorted(missing_snapshots))}",
            )
        )
    report_path = row.get("report_path")
    if not report_path:
        issues.append(EvidenceIssue(session_id, "session_report_missing", "Report path is absent"))
        return issues
    try:
        report = json.loads(Path(str(report_path)).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        issues.append(
            EvidenceIssue(session_id, "session_report_invalid", "Report is missing or invalid")
        )
        return issues
    if str(report.get("session_id")) != session_id:
        issues.append(
            EvidenceIssue(session_id, "session_report_mismatch", "Report session ID does not match")
        )
    if report.get("clean_shutdown") is not True:
        issues.append(
            EvidenceIssue(session_id, "report_shutdown_unclean", "Report is not a clean shutdown")
        )
    if report.get("reconciliation_healthy") is not True:
        issues.append(
            EvidenceIssue(
                session_id, "report_reconciliation_failed", "Report reconciliation is not healthy"
            )
        )
    return issues


def _workflow_blockers(dry: int, formal: int) -> tuple[str, ...]:
    blockers = [
        "Production-authenticated current-data collector/runtime assembly is not yet wired",
        "Operator must verify instrument master, calendar, feed freshness, account, and auth",
    ]
    if dry < DRY_REQUIRED:
        blockers.append(f"{DRY_REQUIRED - dry} audited dry sessions remain")
    if formal < FORMAL_REQUIRED:
        blockers.append(f"{FORMAL_REQUIRED - formal} audited formal sessions remain")
    return tuple(blockers)
