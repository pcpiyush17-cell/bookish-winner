from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_quant.clocks import SimulatedClock
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationError, MigrationRunner


class NaiveClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 28, 10, 0)


def test_migrations_are_idempotent_and_record_clock_time(tmp_path: Path) -> None:
    database = Database(tmp_path / "trading.sqlite")
    current = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    runner = MigrationRunner(database, clock=SimulatedClock(current))

    assert runner.apply_all() == (1, 2, 3, 4)
    assert runner.apply_all() == ()

    connection = database.connect(read_only=True)
    try:
        row = connection.execute(
            "SELECT version, name, applied_at FROM schema_migrations"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == (1, "initial", current.isoformat())


def test_migration_checksum_drift_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    migration = directory / "0001_example.sql"
    migration.write_text("CREATE TABLE example(id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8")
    database = Database(tmp_path / "trading.sqlite")
    runner = MigrationRunner(database, directory)
    runner.apply_all()

    migration.write_text(
        "CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT) STRICT;", encoding="utf-8"
    )

    with pytest.raises(MigrationError) as error:
        runner.apply_all()
    assert error.value.code == "migration_drift"


def test_failed_migration_is_atomic_and_not_recorded(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_good.sql").write_text(
        "CREATE TABLE good(id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8"
    )
    (directory / "0002_bad.sql").write_text("THIS IS NOT SQL;", encoding="utf-8")
    database = Database(tmp_path / "trading.sqlite")

    with pytest.raises(MigrationError) as error:
        MigrationRunner(database, directory).apply_all()
    assert error.value.code == "migration_apply_failed"

    connection = database.connect(read_only=True)
    try:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    finally:
        connection.close()
    assert versions == [1]


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({}, "migrations_empty"),
        ({"bad-name.sql": "SELECT 1;"}, "migration_name_invalid"),
        ({"0002_gap.sql": "SELECT 1;"}, "migration_version_gap"),
    ],
)
def test_invalid_migration_sets_are_rejected(
    tmp_path: Path, files: dict[str, str], code: str
) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8")

    with pytest.raises(MigrationError) as error:
        MigrationRunner(Database(tmp_path / "trading.sqlite"), directory).apply_all()
    assert error.value.code == code


def test_missing_migration_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MigrationError) as error:
        MigrationRunner(Database(tmp_path / "trading.sqlite"), tmp_path / "missing").apply_all()
    assert error.value.code == "migration_directory_missing"


def test_migrations_reject_naive_clock(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_example.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationError) as error:
        MigrationRunner(Database(tmp_path / "trading.sqlite"), directory, NaiveClock()).apply_all()
    assert error.value.code == "migration_clock_naive"
