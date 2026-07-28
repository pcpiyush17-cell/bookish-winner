"""SQLite connection, transaction, integrity, and backup primitives."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class StorageError(RuntimeError):
    """An operational storage failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    """Result returned by SQLite's full database integrity check."""

    passed: bool
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Database:
    """Own SQLite connections and enforce the operational PRAGMA contract."""

    path: Path
    busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.busy_timeout_ms, int)
            or isinstance(self.busy_timeout_ms, bool)
            or self.busy_timeout_ms <= 0
        ):
            raise ValueError("busy_timeout_ms must be a positive integer")

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        """Open one configured connection; callers remain responsible for closing it."""
        resolved = self.path.expanduser().resolve()
        if read_only:
            if not resolved.is_file():
                raise StorageError("database_not_found", f"Database does not exist: {resolved}")
            target = f"{resolved.as_uri()}?mode=ro"
            connection = sqlite3.connect(target, uri=True, isolation_level=None)
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(resolved, isolation_level=None)

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if not read_only:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                connection.close()
                raise StorageError("wal_unavailable", "SQLite could not enable WAL mode")
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Run work in an explicit transaction and always close its connection."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def integrity_check(self) -> IntegrityResult:
        """Run SQLite's full integrity check through a read-only connection."""
        connection = self.connect(read_only=True)
        try:
            messages = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        finally:
            connection.close()
        return IntegrityResult(messages == ("ok",), messages)

    def backup(self, destination: Path) -> Path:
        """Create and verify a consistent backup without overwriting an existing file."""
        source = self.path.expanduser().resolve()
        target = destination.expanduser().resolve()
        if not source.is_file():
            raise StorageError("database_not_found", f"Database does not exist: {source}")
        if source == target:
            raise StorageError(
                "backup_same_path", "Backup destination must differ from the database"
            )
        if target.exists():
            raise StorageError("backup_exists", f"Backup already exists: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.partial-{uuid4().hex}")
        source_connection = self.connect(read_only=True)
        try:
            with closing(sqlite3.connect(partial)) as destination_connection:
                source_connection.backup(destination_connection)
                messages = tuple(
                    str(row[0]) for row in destination_connection.execute("PRAGMA integrity_check")
                )
            if messages != ("ok",):
                raise StorageError("backup_integrity_failed", "Backup failed its integrity check")
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            source_connection.close()
        return target
