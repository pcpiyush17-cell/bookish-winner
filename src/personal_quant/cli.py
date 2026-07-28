"""Command-line interface for explicit operator actions."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from personal_quant import __version__
from personal_quant.clocks import SystemClock
from personal_quant.doctor import run_checks
from personal_quant.storage.database import Database, StorageError
from personal_quant.storage.maintenance import timestamped_backup_path
from personal_quant.storage.migrations import MigrationRunner

app = typer.Typer(
    name="pq",
    help="Operate the Personal Quant Trading System.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    """Print the package version and exit when requested."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Operate the Personal Quant Trading System."""


@app.command()
def doctor() -> None:
    """Validate the local runtime without contacting external services."""
    checks = run_checks()
    for check in checks:
        marker = "PASS" if check.passed else "FAIL"
        typer.echo(f"[{marker}] {check.name}: {check.detail}")

    if not all(check.passed for check in checks):
        raise typer.Exit(code=1)

    typer.echo("Environment is ready for local development.")


@app.command("init-db")
def init_db(
    path: Annotated[
        Path,
        typer.Option("--path", help="SQLite database path."),
    ] = Path("state/trading.sqlite"),
) -> None:
    """Initialize or safely migrate the operational database."""
    try:
        applied = MigrationRunner(Database(path)).apply_all()
    except StorageError as error:
        _storage_failure(error)
    versions = ", ".join(f"{version:04d}" for version in applied) if applied else "none"
    typer.echo(f"Database ready: {path}")
    typer.echo(f"Migrations applied: {versions}")


@app.command("db-check")
def db_check(
    path: Annotated[
        Path,
        typer.Option("--path", help="SQLite database path."),
    ] = Path("state/trading.sqlite"),
) -> None:
    """Run SQLite's full integrity check without modifying the database."""
    try:
        result = Database(path).integrity_check()
    except StorageError as error:
        _storage_failure(error)
    if not result.passed:
        typer.echo(f"[FAIL] Database integrity: {'; '.join(result.messages)}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"[PASS] Database integrity: {path}")


@app.command()
def backup(
    path: Annotated[
        Path,
        typer.Option("--path", help="SQLite database path."),
    ] = Path("state/trading.sqlite"),
    destination: Annotated[
        Path | None,
        typer.Option("--destination", help="Exact non-existing backup path."),
    ] = None,
) -> None:
    """Create an online SQLite backup and verify its integrity."""
    target = destination or timestamped_backup_path(path, Path("backups"), SystemClock())
    try:
        created = Database(path).backup(target)
    except StorageError as error:
        _storage_failure(error)
    typer.echo(f"Backup created and verified: {created}")


def _storage_failure(error: StorageError) -> NoReturn:
    typer.echo(f"Storage error [{error.code}]: {error}", err=True)
    raise typer.Exit(code=1)
