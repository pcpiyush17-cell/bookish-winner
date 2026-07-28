"""Foundational repositories with explicit idempotency semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from personal_quant.clocks import Clock, SystemClock
from personal_quant.config import OperatingMode
from personal_quant.domain.events import CoreEventType, EventMeta
from personal_quant.domain.identifiers import RunId, SessionId
from personal_quant.storage.database import Database


@dataclass(frozen=True, slots=True)
class RuntimeSession:
    session_id: SessionId
    run_id: RunId
    mode: OperatingMode
    config_hash: str
    started_at: datetime
    ended_at: datetime | None
    status: str


@dataclass(frozen=True, slots=True)
class StoredEvent:
    meta: EventMeta
    payload: dict[str, Any]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class RuntimeSessionRepository:
    database: Database

    def start(
        self,
        *,
        session_id: SessionId,
        run_id: RunId,
        mode: OperatingMode,
        config_hash: str,
        started_at: datetime,
    ) -> None:
        """Persist one new runtime session."""
        timestamp = _aware_iso(started_at)
        if len(config_hash) != 64 or any(
            character not in "0123456789abcdef" for character in config_hash
        ):
            raise ValueError("config_hash must be a lowercase SHA-256 hex digest")
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO runtime_sessions(
                    session_id, run_id, mode, config_hash, started_at, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (str(session_id), str(run_id), mode.value, config_hash, timestamp),
            )

    def finish(self, session_id: SessionId, ended_at: datetime) -> bool:
        """Finish a running session once; return whether a row changed."""
        timestamp = _aware_iso(ended_at)
        with self.database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_sessions
                SET ended_at = ?, status = 'stopped'
                WHERE session_id = ? AND status = 'running'
                """,
                (timestamp, str(session_id)),
            )
            return cursor.rowcount == 1

    def get(self, session_id: SessionId) -> RuntimeSession | None:
        """Read a session by identifier."""
        connection = self.database.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM runtime_sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return RuntimeSession(
            session_id=SessionId(UUID(row["session_id"])),
            run_id=RunId(UUID(row["run_id"])),
            mode=OperatingMode(row["mode"]),
            config_hash=row["config_hash"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            status=row["status"],
        )


@dataclass(frozen=True, slots=True)
class EventRepository:
    database: Database
    clock: Clock = field(default_factory=SystemClock)

    def append(self, meta: EventMeta, payload: Mapping[str, object]) -> bool:
        """Append an event once; duplicate event IDs are harmless no-ops."""
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO event_log(
                    event_id, event_type, schema_version, occurred_at, received_at,
                    source, correlation_id, causation_id, run_id, payload_json, payload_hash
                    , recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    str(meta.event_id),
                    meta.event_type.value,
                    meta.schema_version,
                    _aware_iso(meta.occurred_at),
                    _aware_iso(meta.received_at),
                    meta.source,
                    str(meta.correlation_id) if meta.correlation_id else None,
                    str(meta.causation_id) if meta.causation_id else None,
                    str(meta.run_id),
                    canonical,
                    payload_hash,
                    _aware_iso(self.clock.now()),
                ),
            )
            return cursor.rowcount == 1

    def get(self, event_id: UUID) -> StoredEvent | None:
        """Read an event by its globally unique identifier."""
        connection = self.database.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM event_log WHERE event_id = ?", (str(event_id),)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        meta = EventMeta(
            event_id=UUID(row["event_id"]),
            event_type=CoreEventType(row["event_type"]),
            schema_version=row["schema_version"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            source=row["source"],
            correlation_id=UUID(row["correlation_id"]) if row["correlation_id"] else None,
            causation_id=UUID(row["causation_id"]) if row["causation_id"] else None,
            run_id=RunId(UUID(row["run_id"])),
        )
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("stored event payload must be a JSON object")
        return StoredEvent(
            meta=meta,
            payload=payload,
            payload_hash=row["payload_hash"],
        )


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()
