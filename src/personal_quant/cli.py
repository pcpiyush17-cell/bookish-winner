"""Command-line interface for explicit operator actions."""

import os
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, NoReturn
from zoneinfo import ZoneInfo

import typer

from personal_quant import __version__
from personal_quant.broker.auth import (
    BrokerAuthenticationError,
    SandboxAuthenticator,
    TokenStore,
)
from personal_quant.broker.contracts import BrokerError
from personal_quant.broker.sandbox import create_sandbox_client
from personal_quant.clocks import SystemClock
from personal_quant.costs import CostConfig, CostEngine, CostError, DeliveryTrade
from personal_quant.doctor import run_checks
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.historical import (
    HistoricalDataError,
    HistoricalIngestor,
    HistoricalRateLimiter,
    HistoricalRequest,
    KiteHistoricalSource,
)
from personal_quant.instruments import (
    InstrumentError,
    InstrumentSnapshotStore,
    SandboxKiteInstrumentSource,
    download_instruments,
)
from personal_quant.market_calendar import CalendarError, MarketCalendar
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


@app.command("instruments-download")
def instruments_download(
    root: Annotated[Path, typer.Option("--root", help="Instrument snapshot root.")] = Path(
        "data/reference/instruments"
    ),
    token_path: Annotated[Path, typer.Option("--token-path", help="Sandbox token file.")] = Path(
        "state/session/sandbox_access_token.json"
    ),
) -> None:
    """Download and persist today's validated NSE equity instrument snapshot."""
    api_key = os.environ.get("KITE_SANDBOX_API_KEY")
    if not api_key:
        _broker_failure(
            BrokerAuthenticationError(
                "sandbox_api_key_missing", "KITE_SANDBOX_API_KEY is not configured"
            )
        )
    try:
        token = TokenStore(token_path).load()
        client = create_sandbox_client(api_key)
        client.set_access_token(token.access_token)
        now = SystemClock().now()
        snapshot = download_instruments(
            SandboxKiteInstrumentSource(client),
            InstrumentSnapshotStore(root),
            snapshot_date=now.astimezone(ZoneInfo("Asia/Kolkata")).date(),
            downloaded_at=now,
        )
    except StorageError as error:
        _storage_failure(error)
    except (InstrumentError, BrokerError) as error:
        _reference_failure(error)
    typer.echo(f"Instrument snapshot ready: {snapshot.manifest.row_count} NSE equities")
    typer.echo(f"SHA-256: {snapshot.manifest.checksum_sha256}")


@app.command("instruments-validate")
def instruments_validate(
    directory: Annotated[Path, typer.Option("--directory", help="Dated snapshot directory.")],
) -> None:
    """Verify a stored instrument snapshot checksum and schema."""
    try:
        snapshot = InstrumentSnapshotStore(directory).load(directory)
    except InstrumentError as error:
        _reference_failure(error)
    typer.echo(f"[PASS] Instrument snapshot: {snapshot.manifest.row_count} rows")
    typer.echo(f"SHA-256: {snapshot.manifest.checksum_sha256}")


@app.command("calendar-check")
def calendar_check(
    day: Annotated[str, typer.Option("--date", help="Date in YYYY-MM-DD format.")],
    config: Annotated[Path, typer.Option("--config", help="Versioned calendar YAML.")] = Path(
        "config/calendars/nse_equity_2026.yaml"
    ),
) -> None:
    """Report whether an explicitly configured date is an NSE trading day."""
    try:
        query = date.fromisoformat(day)
        calendar = MarketCalendar.load(config)
        trading = calendar.is_trading_day(query)
    except CalendarError as error:
        _reference_failure(error)
    except ValueError:
        _reference_failure(CalendarError("calendar_date_invalid", "Date must use YYYY-MM-DD"))
    state = "TRADING" if trading else "CLOSED"
    typer.echo(f"{query.isoformat()}: {state} ({calendar.config.calendar_id})")


@app.command("historical-download")
def historical_download(
    instrument: Annotated[str, typer.Option("--instrument", help="Durable NSE:SYMBOL key.")],
    start: Annotated[str, typer.Option("--start", help="Inclusive ISO date.")],
    end: Annotated[str, typer.Option("--end", help="Inclusive ISO date.")],
    snapshot: Annotated[Path, typer.Option("--snapshot", help="Dated instrument directory.")],
    interval: Annotated[str, typer.Option("--interval", help="day or 15minute.")] = "day",
    root: Annotated[Path, typer.Option("--root", help="Historical data root.")] = Path("data"),
    token_path: Annotated[Path, typer.Option("--token-path", help="Sandbox token file.")] = Path(
        "state/session/sandbox_access_token.json"
    ),
    calendar_path: Annotated[Path, typer.Option("--calendar", help="Market calendar YAML.")] = Path(
        "config/calendars/nse_equity_2026.yaml"
    ),
) -> None:
    """Download one idempotent, validated historical candle batch."""
    api_key = os.environ.get("KITE_SANDBOX_API_KEY")
    if not api_key:
        _broker_failure(
            BrokerAuthenticationError(
                "sandbox_api_key_missing", "KITE_SANDBOX_API_KEY is not configured"
            )
        )
    try:
        zone = ZoneInfo("Asia/Kolkata")
        start_at = datetime.combine(date.fromisoformat(start), time.min, zone)
        end_at = datetime.combine(date.fromisoformat(end), time.max, zone)
        stored = TokenStore(token_path).load()
        client = create_sandbox_client(api_key)
        client.set_access_token(stored.access_token)
        master = InstrumentSnapshotStore(snapshot).load(snapshot)
        key = InstrumentKey(instrument)
        request = HistoricalRequest(key, master.resolve_token(key), interval, start_at, end_at)
        clock = SystemClock()
        result = HistoricalIngestor(
            KiteHistoricalSource(client),
            HistoricalRateLimiter(clock),
            MarketCalendar.load(calendar_path),
            root,
            clock,
        ).ingest(request)
    except StorageError as error:
        _storage_failure(error)
    except (InstrumentError, CalendarError, HistoricalDataError, BrokerError) as error:
        _reference_failure(error)
    except ValueError:
        _reference_failure(
            HistoricalDataError("historical_date_invalid", "Dates must use YYYY-MM-DD")
        )
    typer.echo(f"Historical batch: {result.status}")
    typer.echo(
        f"Raw={result.raw_rows} Curated={result.curated_rows} "
        f"Invalid={result.invalid_rows} Gaps={len(result.gaps)}"
    )
    typer.echo(f"Manifest: {result.manifest_path}")


@app.command("cost-estimate")
def cost_estimate(
    quantity: Annotated[int, typer.Option("--quantity", min=1)],
    buy_price: Annotated[str, typer.Option("--buy-price")],
    sell_price: Annotated[str, typer.Option("--sell-price")],
    spread_bps: Annotated[str, typer.Option("--spread-bps")] = "0",
    slippage_bps: Annotated[str, typer.Option("--slippage-bps")] = "0",
    impact_bps: Annotated[str, typer.Option("--impact-bps")] = "0",
    config: Annotated[Path, typer.Option("--config", help="Versioned charge YAML.")] = Path(
        "config/costs/zerodha_nse_delivery_2026-07-28.yaml"
    ),
) -> None:
    """Estimate delivery costs under base, 1.5x, and 2.0x execution scenarios."""
    try:
        trade = DeliveryTrade(
            quantity,
            _parse_decimal(buy_price),
            _parse_decimal(sell_price),
            _parse_decimal(spread_bps),
            _parse_decimal(slippage_bps),
            _parse_decimal(impact_bps),
        )
        scenarios = CostEngine(CostConfig.load(config)).stress_scenarios(trade)
    except CostError as error:
        _reference_failure(error)
    for name, result in scenarios.items():
        typer.echo(
            f"{name}: costs=INR {result.variable_total} "
            f"gross=INR {result.gross_pnl} net=INR {result.trading_net_pnl}"
        )
    typer.echo(f"Calculation version: {scenarios['base'].calculation_version}")


@app.command("break-even")
def break_even(
    capital: Annotated[str, typer.Option("--capital")],
    variable_costs: Annotated[str, typer.Option("--variable-costs")],
    target_profit: Annotated[str, typer.Option("--target-profit")] = "0",
    config: Annotated[Path, typer.Option("--config", help="Versioned charge YAML.")] = Path(
        "config/costs/zerodha_nse_delivery_2026-07-28.yaml"
    ),
) -> None:
    """Calculate monthly gross break-even requirements."""
    try:
        report = CostEngine(CostConfig.load(config)).break_even(
            starting_capital=_parse_decimal(capital),
            expected_variable_costs=_parse_decimal(variable_costs),
            target_net_profit=_parse_decimal(target_profit),
        )
    except CostError as error:
        _reference_failure(error)
    typer.echo(f"Break-even: INR {report.break_even_rupees}")
    typer.echo(f"Required monthly gross return: {report.required_monthly_gross_return_pct}%")
    typer.echo(f"Calculation version: {report.calculation_version}")


def _storage_failure(error: StorageError) -> NoReturn:
    typer.echo(f"Storage error [{error.code}]: {error}", err=True)
    raise typer.Exit(code=1)


def _broker_failure(error: BrokerAuthenticationError | StorageError) -> NoReturn:
    code = error.code
    typer.echo(f"Broker error [{code}]: {error}", err=True)
    raise typer.Exit(code=1)


def _reference_failure(
    error: InstrumentError | CalendarError | HistoricalDataError | CostError | BrokerError,
) -> NoReturn:
    typer.echo(f"Reference data error [{error.code}]: {error}", err=True)
    raise typer.Exit(code=1)


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise CostError("cost_number_invalid", "Cost inputs must be decimal numbers") from error
    if not parsed.is_finite():
        raise CostError("cost_number_invalid", "Cost inputs must be finite decimal numbers")
    return parsed
