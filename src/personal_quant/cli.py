"""Command-line interface for explicit operator actions."""

import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from personal_quant import __version__
from personal_quant.broker.auth import (
    BrokerAuthenticationError,
    SandboxAuthenticator,
    TokenStore,
)
from personal_quant.broker.sandbox import create_sandbox_client
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


@app.command("kite-login")
def kite_login(
    exchange: Annotated[
        bool,
        typer.Option(
            "--exchange",
            help="Prompt for a request token and complete sandbox authentication.",
        ),
    ] = False,
    token_path: Annotated[
        Path,
        typer.Option("--token-path", help="Restricted sandbox token file."),
    ] = Path("state/session/sandbox_access_token.json"),
) -> None:
    """Perform human-mediated Kite sandbox login; production login is unavailable."""
    api_key = os.environ.get("KITE_SANDBOX_API_KEY")
    if not api_key:
        _broker_failure(
            BrokerAuthenticationError(
                "sandbox_api_key_missing", "KITE_SANDBOX_API_KEY is not configured"
            )
        )
    client = create_sandbox_client(api_key)
    authenticator = SandboxAuthenticator(client, SystemClock(), TokenStore(token_path))
    typer.echo(f"Sandbox login URL: {authenticator.login_url()}")
    typer.echo("Complete login and two-factor authentication in your browser.")
    if not exchange:
        typer.echo("Then rerun with --exchange to enter the short-lived request token.")
        return

    api_secret = os.environ.get("KITE_SANDBOX_API_SECRET")
    expected_user = os.environ.get("KITE_SANDBOX_EXPECTED_USER_ID")
    if not api_secret or not expected_user:
        _broker_failure(
            BrokerAuthenticationError(
                "sandbox_auth_config_missing",
                "Sandbox API secret and expected user ID must be configured",
            )
        )
    request_token = typer.prompt("Request token", hide_input=True)
    try:
        session = authenticator.exchange(
            request_token=request_token,
            api_secret=api_secret,
            expected_user_id=expected_user,
        )
    except (BrokerAuthenticationError, StorageError) as error:
        _broker_failure(error)
    typer.echo(f"Sandbox authenticated for user: {session.profile.user_id}")
    typer.echo(f"Session token stored at: {session.token_path}")


def _storage_failure(error: StorageError) -> NoReturn:
    typer.echo(f"Storage error [{error.code}]: {error}", err=True)
    raise typer.Exit(code=1)


def _broker_failure(error: BrokerAuthenticationError | StorageError) -> NoReturn:
    code = error.code
    typer.echo(f"Broker error [{code}]: {error}", err=True)
    raise typer.Exit(code=1)
