import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from personal_quant.clocks import SimulatedClock
from personal_quant.config import OperatingMode
from personal_quant.domain.events import CoreEventType, EventMeta
from personal_quant.domain.identifiers import RunId, SessionId
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner
from personal_quant.storage.repositories import EventRepository, RuntimeSessionRepository


def initialized_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    return database


def test_runtime_session_survives_clean_restart(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    repository = RuntimeSessionRepository(database)
    session_id = SessionId(uuid4())
    run_id = RunId(uuid4())
    started = datetime(2026, 7, 28, 3, 30, tzinfo=UTC)
    repository.start(
        session_id=session_id,
        run_id=run_id,
        mode=OperatingMode.PAPER,
        config_hash="a" * 64,
        started_at=started,
    )

    reopened = RuntimeSessionRepository(Database(database.path))
    session = reopened.get(session_id)

    assert session is not None
    assert session.run_id == run_id
    assert session.mode is OperatingMode.PAPER
    assert session.status == "running"
    assert session.started_at == started
    assert reopened.finish(session_id, started + timedelta(hours=1))
    assert not reopened.finish(session_id, started + timedelta(hours=2))
    assert reopened.get(SessionId(uuid4())) is None


def test_runtime_session_cannot_finish_before_it_started(tmp_path: Path) -> None:
    repository = RuntimeSessionRepository(initialized_database(tmp_path))
    session_id = SessionId(uuid4())
    started = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    repository.start(
        session_id=session_id,
        run_id=RunId(uuid4()),
        mode=OperatingMode.PAPER,
        config_hash="a" * 64,
        started_at=started,
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.finish(session_id, started - timedelta(seconds=1))


def test_runtime_session_validates_hash_and_time(tmp_path: Path) -> None:
    repository = RuntimeSessionRepository(initialized_database(tmp_path))
    values = {
        "session_id": SessionId(uuid4()),
        "run_id": RunId(uuid4()),
        "mode": OperatingMode.PAPER,
        "config_hash": "invalid",
        "started_at": datetime(2026, 7, 28, 3, 30, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match="config_hash"):
        repository.start(**values)  # type: ignore[arg-type]

    values["config_hash"] = "a" * 64
    values["started_at"] = datetime(2026, 7, 28, 3, 30)
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.start(**values)  # type: ignore[arg-type]


def test_event_append_is_idempotent_and_round_trips(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    recorded = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    repository = EventRepository(database, SimulatedClock(recorded))
    event_id = uuid4()
    run_id = RunId(uuid4())
    meta = EventMeta(
        event_id=event_id,
        event_type=CoreEventType.RUNTIME_STARTED,
        schema_version=1,
        occurred_at=recorded,
        received_at=recorded,
        source="test-runtime",
        correlation_id=uuid4(),
        causation_id=None,
        run_id=run_id,
    )

    assert repository.append(meta, {"mode": "paper", "sequence": 1})
    assert not repository.append(meta, {"mode": "different"})

    stored = EventRepository(Database(database.path)).get(event_id)
    assert stored is not None
    assert stored.meta == meta
    assert stored.payload == {"mode": "paper", "sequence": 1}
    assert len(stored.payload_hash) == 64
    assert repository.get(uuid4()) is None


def test_event_repository_rejects_naive_clock(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    clock = SimulatedClock(datetime(2026, 7, 28, 10, 0, tzinfo=UTC))
    repository = EventRepository(database, clock)
    meta = EventMeta(
        event_id=uuid4(),
        event_type=CoreEventType.RUNTIME_STARTED,
        schema_version=1,
        occurred_at=clock.now(),
        received_at=clock.now(),
        source="test",
        correlation_id=None,
        causation_id=None,
        run_id=RunId(uuid4()),
    )

    clock._current = datetime(2026, 7, 28, 10, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.append(meta, {})


def test_event_repository_rejects_non_json_numbers(tmp_path: Path) -> None:
    current = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    repository = EventRepository(initialized_database(tmp_path), SimulatedClock(current))
    meta = EventMeta(
        event_id=uuid4(),
        event_type=CoreEventType.RUNTIME_STARTED,
        schema_version=1,
        occurred_at=current,
        received_at=current,
        source="test",
        correlation_id=None,
        causation_id=None,
        run_id=RunId(uuid4()),
    )

    with pytest.raises(ValueError, match="Out of range float"):
        repository.append(meta, {"invalid": float("nan")})
