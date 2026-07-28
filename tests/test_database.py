import sqlite3
from pathlib import Path

import pytest

from personal_quant.storage.database import Database, StorageError
from personal_quant.storage.migrations import MigrationRunner


def initialized_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "trading.sqlite")
    MigrationRunner(database).apply_all()
    return database


def test_connections_enforce_operational_pragmas(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    connection = database.connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    finally:
        connection.close()


def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    with database.transaction(write=True) as connection:
        connection.execute("CREATE TABLE transaction_test(value TEXT NOT NULL) STRICT")

    with (
        pytest.raises(RuntimeError, match="force rollback"),
        database.transaction(write=True) as connection,
    ):
        connection.execute("INSERT INTO transaction_test(value) VALUES ('uncommitted')")
        raise RuntimeError("force rollback")

    connection = database.connect(read_only=True)
    try:
        count = connection.execute("SELECT count(*) FROM transaction_test").fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_integrity_check_requires_existing_database(tmp_path: Path) -> None:
    with pytest.raises(StorageError) as error:
        Database(tmp_path / "missing.sqlite").integrity_check()

    assert error.value.code == "database_not_found"


def test_backup_is_verified_and_does_not_overwrite(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    destination = tmp_path / "backups" / "snapshot.sqlite"

    assert database.backup(destination) == destination.resolve()
    assert Database(destination).integrity_check().passed

    with pytest.raises(StorageError) as error:
        database.backup(destination)
    assert error.value.code == "backup_exists"


def test_backup_rejects_missing_source_and_same_path(tmp_path: Path) -> None:
    missing = Database(tmp_path / "missing.sqlite")
    with pytest.raises(StorageError) as missing_error:
        missing.backup(tmp_path / "backup.sqlite")
    assert missing_error.value.code == "database_not_found"

    database = initialized_database(tmp_path)
    with pytest.raises(StorageError) as same_path_error:
        database.backup(database.path)
    assert same_path_error.value.code == "backup_same_path"


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = initialized_database(tmp_path)
    with database.transaction(write=True) as connection:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY) STRICT")
        connection.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id)) STRICT")

    with pytest.raises(sqlite3.IntegrityError), database.transaction(write=True) as connection:
        connection.execute("INSERT INTO child(parent_id) VALUES (999)")


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_busy_timeout_must_be_positive_integer(tmp_path: Path, timeout: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Database(tmp_path / "trading.sqlite", busy_timeout_ms=timeout)
