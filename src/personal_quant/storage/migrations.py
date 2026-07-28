"""Numbered, checksummed SQLite schema migrations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

from personal_quant.clocks import Clock, SystemClock
from personal_quant.storage.database import Database, StorageError

_MIGRATION_FILE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(StorageError):
    """A migration discovery, drift, or application failure."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationRunner:
    database: Database
    directory: Path | None = None
    clock: Clock = field(default_factory=SystemClock)

    def apply_all(self) -> tuple[int, ...]:
        """Apply every pending migration once and return the applied versions."""
        migrations = self._discover()
        self._ensure_migration_table()
        applied = self._applied()

        for migration in migrations:
            existing_checksum = applied.get(migration.version)
            if existing_checksum is not None and existing_checksum != migration.checksum:
                raise MigrationError(
                    "migration_drift",
                    f"Applied migration {migration.version:04d} no longer matches its checksum",
                )

        newly_applied: list[int] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            self._apply(migration)
            newly_applied.append(migration.version)
        return tuple(newly_applied)

    def _migration_directory(self) -> Path:
        return self.directory or Path(__file__).with_name("sql")

    def _discover(self) -> tuple[Migration, ...]:
        directory = self._migration_directory()
        if not directory.is_dir():
            raise MigrationError("migration_directory_missing", f"Missing migrations: {directory}")

        migrations: list[Migration] = []
        for path in sorted(directory.glob("*.sql")):
            match = _MIGRATION_FILE.fullmatch(path.name)
            if match is None:
                raise MigrationError(
                    "migration_name_invalid", f"Invalid migration name: {path.name}"
                )
            sql = path.read_text(encoding="utf-8")
            migrations.append(
                Migration(
                    version=int(match.group("version")),
                    name=match.group("name"),
                    sql=sql,
                    checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                )
            )

        versions = [migration.version for migration in migrations]
        if not versions:
            raise MigrationError("migrations_empty", "No migration files were found")
        if len(versions) != len(set(versions)):
            raise MigrationError("migration_version_duplicate", "Migration versions must be unique")
        if versions != list(range(1, len(versions) + 1)):
            raise MigrationError("migration_version_gap", "Migration versions must be contiguous")
        return tuple(migrations)

    def _ensure_migration_table(self) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                ) STRICT
                """
            )

    def _applied(self) -> dict[int, str]:
        connection = self.database.connect(read_only=True)
        try:
            rows = connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            return {int(row["version"]): str(row["checksum"]) for row in rows}
        finally:
            connection.close()

    def _apply(self, migration: Migration) -> None:
        connection = self.database.connect()
        safe_name = migration.name.replace("'", "''")
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            connection.close()
            raise MigrationError("migration_clock_naive", "Migration clock must be timezone-aware")
        applied_at = now.astimezone(UTC).isoformat().replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES "
            f"({migration.version}, '{safe_name}', '{migration.checksum}', '{applied_at}');\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise MigrationError(
                "migration_apply_failed",
                f"Failed to apply migration {migration.version:04d}_{migration.name}",
            ) from error
        finally:
            connection.close()
