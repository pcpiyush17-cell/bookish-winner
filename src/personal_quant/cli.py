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
from personal_quant.broker.production import ProductionAuthenticator, create_production_client
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
from personal_quant.historical_paper import (
    HistoricalPaperConfig,
    HistoricalPaperError,
    run_historical_paper_session,
)
from personal_quant.instruments import (
    InstrumentError,
    InstrumentSnapshotStore,
    InstrumentSource,
    ProductionKiteInstrumentSource,
    SandboxKiteInstrumentSource,
    download_instruments,
)
from personal_quant.market_calendar import CalendarError, MarketCalendar
from personal_quant.paper_evidence import audit_hybrid_evidence, audit_paper_evidence
from personal_quant.paper_runner import PaperRunnerError, RunnerConfig, build_operational_runner
from personal_quant.paper_runtime import RuntimeError as PaperRuntimeError
from personal_quant.research_benchmarks import BenchmarkSuiteConfig, ResearchBenchmarkError
from personal_quant.research_experiments import (
    ExperimentRegistry,
    ResearchExperimentError,
)
from personal_quant.research_governance import (
    ExperimentManifest,
    ResearchGovernance,
    ResearchGovernanceError,
)
from personal_quant.research_momentum import MomentumConfig, ResearchMomentumError
from personal_quant.research_universe import (
    PointInTimeUniverseStore,
    ResearchUniverseError,
    UniverseQualityPolicy,
    discover_snapshot_directories,
    validate_cli_paths,
)
from personal_quant.risk import KillSwitch, RiskError
from personal_quant.storage.database import Database, StorageError
from personal_quant.storage.maintenance import timestamped_backup_path
from personal_quant.storage.migrations import MigrationRunner

app = typer.Typer(
    name="pq",
    help="Operate the Personal Quant Trading System.",
    no_args_is_help=True,
)


@app.command("research-momentum-check")
def research_momentum_check(
    config_path: Annotated[
        Path, typer.Option("--config", help="Versioned cross-sectional momentum configuration.")
    ] = Path("config/research/cross_sectional_momentum_v1.yaml"),
) -> None:
    """Validate the QR-04 challenger contract without executing research."""
    try:
        config = MomentumConfig.load(config_path)
    except ResearchMomentumError as error:
        typer.echo(f"Research momentum error [{error.code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Research momentum valid: {config.strategy_id}")
    typer.echo("Signal execution lag: 1 observation")
    typer.echo("Selection window: validation only")
    typer.echo("Eligible for operational promotion: NO")
    typer.echo("Production order routing: disabled")


@app.command("research-benchmarks-check")
def research_benchmarks_check(
    config_path: Annotated[
        Path, typer.Option("--config", help="Versioned research benchmark suite configuration.")
    ] = Path("config/research/benchmarks_v1.yaml"),
) -> None:
    """Validate benchmark controls without executing research or changing state."""
    try:
        config = BenchmarkSuiteConfig.load(config_path)
    except ResearchBenchmarkError as error:
        typer.echo(f"Research benchmark error [{error.code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Research benchmark suite valid: {config.suite_id}")
    typer.echo(f"Controls: {len(config.benchmarks)}")
    typer.echo("Selection window: validation only")
    typer.echo("Eligible for operational promotion: NO")
    typer.echo("Production order routing: disabled")


@app.command("research-result-check")
def research_result_check(
    manifest_path: Annotated[
        Path, typer.Option("--manifest", help="Immutable research result manifest JSON.")
    ],
    registry_root: Annotated[
        Path, typer.Option("--registry-root", help="Research experiment registry root.")
    ] = Path("research/results/experiments"),
    holdout_state_root: Annotated[
        Path, typer.Option("--holdout-state-root", help="Research-only holdout claim root.")
    ] = Path("state/research/holdout"),
) -> None:
    """Verify a research result without promoting it or changing state."""
    try:
        result = ExperimentRegistry(registry_root, holdout_state_root).verify_result(
            manifest_path, project_root=Path.cwd()
        )
    except ResearchExperimentError as error:
        typer.echo(f"Research result error [{error.code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"[PASS] Research result: {result.experiment_id}")
    typer.echo(f"Decision: {result.decision}")
    typer.echo("Eligible for operational promotion: NO")
    typer.echo("Production order routing: disabled")


@app.command("research-universe-build")
def research_universe_build(
    snapshots_root: Annotated[
        Path, typer.Option("--snapshots-root", help="Immutable instrument snapshot root.")
    ] = Path("data/reference/instruments"),
    output_root: Annotated[
        Path, typer.Option("--output-root", help="Research-only universe artifact root.")
    ] = Path("research/data/universes"),
    policy_path: Annotated[
        Path, typer.Option("--policy", help="Versioned universe quality policy.")
    ] = Path("config/research/universe_quality_v1.yaml"),
) -> None:
    """Build an immutable exact-date NSE research universe without routing orders."""
    try:
        validate_cli_paths(
            project_root=Path.cwd(), snapshots_root=snapshots_root, output_root=output_root
        )
        universe = PointInTimeUniverseStore(output_root).build(
            snapshot_directories=discover_snapshot_directories(snapshots_root),
            policy=UniverseQualityPolicy.load(policy_path),
            created_at=SystemClock().now(),
            project_root=Path.cwd(),
        )
    except (ResearchUniverseError, InstrumentError) as error:
        code = getattr(error, "code", "research_universe_failed")
        typer.echo(f"Research universe error [{code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Research universe ready: {universe.manifest.universe_id}")
    typer.echo(f"Observations: {universe.manifest.observation_count}")
    typer.echo(f"Data SHA-256: {universe.manifest.data_sha256}")
    typer.echo("Exact-date membership: enforced")
    typer.echo("WP-14 mutation: disabled")
    typer.echo("Production order routing: disabled")


@app.command("research-universe-check")
def research_universe_check(
    manifest_path: Annotated[
        Path, typer.Option("--manifest", help="Generated universe manifest JSON.")
    ],
) -> None:
    """Verify a research universe checksum and exact-date contract without mutation."""
    try:
        universe = PointInTimeUniverseStore(manifest_path.parent.parent).load(
            manifest_path, project_root=Path.cwd()
        )
    except ResearchUniverseError as error:
        typer.echo(f"Research universe error [{error.code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"[PASS] Research universe: {universe.manifest.universe_id}")
    typer.echo(f"Observations: {universe.manifest.observation_count}")
    typer.echo("Exact-date membership: enforced")
    typer.echo("Production order routing: disabled")


@app.command("research-governance-check")
def research_governance_check(
    config_path: Annotated[
        Path, typer.Option("--config", help="Versioned research governance configuration.")
    ] = Path("config/research/governance_v1.yaml"),
) -> None:
    """Validate research isolation without creating state or experiments."""
    try:
        governance = ResearchGovernance.load(config_path)
        governance.validate_boundaries(Path.cwd())
    except ResearchGovernanceError as error:
        typer.echo(f"Research governance error [{error.code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Research governance valid: {governance.governance_id}")
    typer.echo(f"Workspace root: {governance.workspace_root}")
    typer.echo(f"State root: {governance.state_root}")
    typer.echo("WP-14 mutation: disabled")
    typer.echo("Production order routing: disabled")


@app.command("research-manifest-check")
def research_manifest_check(
    manifest_path: Annotated[
        Path, typer.Option("--manifest", help="Research experiment YAML manifest.")
    ],
) -> None:
    """Validate an experiment intent without running research or writing artifacts."""
    try:
        manifest = ExperimentManifest.load(manifest_path)
    except ResearchGovernanceError as error:
        typer.echo(f"Research manifest error [{error.code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Research manifest valid: {manifest.experiment_id}")
    typer.echo(f"Strategy family: {manifest.strategy_family}")
    typer.echo(f"Status: {manifest.status}")
    typer.echo("Eligible for operational promotion: NO")


@app.command("historical-paper-session")
def historical_paper_session(
    config_path: Annotated[
        Path, typer.Option("--config", help="Historical paper-session configuration.")
    ] = Path("config/historical_paper.example.yaml"),
    confirm: Annotated[
        str,
        typer.Option(help="Exact phrase: START HISTORICAL PAPER REPLAY."),
    ] = "",
) -> None:
    """Replay one historical market date through PaperBroker without live connectivity."""
    if confirm != "START HISTORICAL PAPER REPLAY":
        typer.echo(
            "Historical paper error [historical_paper_confirmation_invalid]: "
            "Exact confirmation required: START HISTORICAL PAPER REPLAY",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        result = run_historical_paper_session(HistoricalPaperConfig.load(config_path))
    except (HistoricalPaperError, PaperRuntimeError, StorageError) as error:
        code = getattr(error, "code", "historical_paper_failed")
        typer.echo(f"Historical paper error [{code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Historical paper session: {result.session_id}")
    typer.echo(f"Market date: {result.market_date.isoformat()}")
    typer.echo(f"Bars replayed: {result.bars}")
    typer.echo(f"Source SHA-256: {result.source_checksum}")
    typer.echo(f"Report: {result.report_path}")
    typer.echo("This replay is not live-market operational evidence.")


@app.command("hybrid-evidence-status")
def hybrid_evidence_status(
    operational_path: Annotated[
        Path, typer.Option("--operational-path", help="Live operational database path.")
    ] = Path("state/trading.sqlite"),
    replay_path: Annotated[
        Path, typer.Option("--replay-path", help="Historical replay database path.")
    ] = Path("state/replay/trading.sqlite"),
) -> None:
    """Audit the revised WP-14 hybrid acceptance without mutating evidence."""
    try:
        audit = audit_hybrid_evidence(Database(operational_path), Database(replay_path))
    except StorageError as error:
        _storage_failure(error)
    typer.echo(f"Audited live dry sessions: {audit.live_dry_sessions}/5")
    typer.echo(f"Audited historical replay sessions: {audit.replay_sessions}/30")
    typer.echo("WP-14 hybrid acceptance: " + ("MET" if audit.acceptance_met else "PENDING"))
    for blocker in audit.blockers:
        typer.echo(f"Blocker: {blocker}")


@app.command("paper-evidence-status")
def paper_evidence_status(
    path: Annotated[Path, typer.Option("--path", help="SQLite database path.")] = Path(
        "state/trading.sqlite"
    ),
) -> None:
    """Audit WP-14 evidence in read-only mode; never creates or promotes sessions."""
    try:
        audit = audit_paper_evidence(Database(path))
    except StorageError as error:
        _storage_failure(error)
    typer.echo(f"Audited dry sessions: {audit.successful_dry_sessions}/10")
    typer.echo(f"Audited formal sessions: {audit.successful_formal_sessions}/30")
    typer.echo(
        "WP-14 operational acceptance: "
        + ("MET" if audit.operational_acceptance_met else "PENDING")
    )
    for issue in audit.issues:
        typer.echo(f"Issue [{issue.code}] session={issue.session_id}: {issue.message}")
    for blocker in audit.blockers:
        typer.echo(f"Blocker: {blocker}")


@app.command("paper-session-start")
def paper_session_start(
    config_path: Annotated[
        Path, typer.Option("--config", help="Operational paper-runner configuration.")
    ] = Path("config/paper_runner.example.yaml"),
    confirm: Annotated[
        str,
        typer.Option(
            "--confirm",
            help="Exact phrase: START DRY PAPER SESSION or START FORMAL PAPER SESSION.",
        ),
    ] = "",
) -> None:
    """Run one current-data session whose only execution venue is PaperBroker."""
    try:
        config = RunnerConfig.load(config_path)
        expected = f"START {config.evidence_kind.value.upper()} PAPER SESSION"
        if confirm != expected:
            raise PaperRunnerError(
                "paper_confirmation_invalid", f"Exact confirmation required: {expected}"
            )
        runner = build_operational_runner(config)
        typer.echo("Paper runner assembled. Production order routing is not reachable.")
        typer.echo("Press Ctrl+C for graceful shutdown.")
        try:
            report, recording = runner.run_until_stopped()
        except KeyboardInterrupt:
            typer.echo("Graceful shutdown completed after operator interrupt.")
            return
    except (PaperRunnerError, StorageError) as error:
        code = error.code
        typer.echo(f"Paper runner error [{code}]: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Session report: {report.session_id}")
    typer.echo(f"Recording manifest: {recording.manifest_path}")


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


@app.command("kite-production-login")
def kite_production_login(
    exchange: Annotated[
        bool,
        typer.Option(
            "--exchange",
            help="Prompt for a request token and complete production authentication.",
        ),
    ] = False,
    token_path: Annotated[
        Path,
        typer.Option("--token-path", help="Restricted production token file."),
    ] = Path("state/session/production_access_token.json"),
) -> None:
    """Authenticate and validate production identity without enabling order routing."""
    api_key = os.environ.get("KITE_API_KEY")
    if not api_key:
        _broker_failure(
            BrokerAuthenticationError(
                "production_api_key_missing", "KITE_API_KEY is not configured"
            )
        )
    client = create_production_client(api_key)
    authenticator = ProductionAuthenticator(client, SystemClock(), TokenStore(token_path))
    typer.echo(f"Production login URL: {authenticator.login_url()}")
    typer.echo("Complete login and two-factor authentication in your browser.")
    typer.echo("This command cannot enable production order routing.")
    if not exchange:
        typer.echo("Then rerun with --exchange to enter the short-lived request token.")
        return

    api_secret = os.environ.get("KITE_API_SECRET")
    expected_user = os.environ.get("KITE_EXPECTED_USER_ID")
    if not api_secret or not expected_user:
        _broker_failure(
            BrokerAuthenticationError(
                "production_auth_config_missing",
                "Production API secret and expected user ID must be configured",
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
    typer.echo(f"Production identity validated for user: {session.profile.user_id}")
    typer.echo(f"Session token stored at: {session.token_path}")
    typer.echo(f"Token expires by: {session.expires_at.isoformat()}")
    typer.echo("Production order routing remains disabled.")


@app.command("instruments-download")
def instruments_download(
    root: Annotated[Path, typer.Option("--root", help="Instrument snapshot root.")] = Path(
        "data/reference/instruments"
    ),
    token_path: Annotated[
        Path | None,
        typer.Option("--token-path", help="Session token file; defaults depend on mode."),
    ] = None,
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Use authenticated production market data; never enables order routing.",
        ),
    ] = False,
) -> None:
    """Download and persist today's validated NSE equity instrument snapshot."""
    api_key_env = "KITE_API_KEY" if production else "KITE_SANDBOX_API_KEY"
    api_key = os.environ.get(api_key_env)
    if not api_key:
        code = "production_api_key_missing" if production else "sandbox_api_key_missing"
        _broker_failure(BrokerAuthenticationError(code, f"{api_key_env} is not configured"))
    selected_token_path = token_path or Path(
        "state/session/production_access_token.json"
        if production
        else "state/session/sandbox_access_token.json"
    )
    try:
        token = TokenStore(selected_token_path).load()
        source: InstrumentSource
        if production:
            expected_user = os.environ.get("KITE_EXPECTED_USER_ID")
            if not expected_user:
                raise BrokerAuthenticationError(
                    "production_expected_user_missing",
                    "KITE_EXPECTED_USER_ID is not configured",
                )
            if token.user_id != expected_user:
                raise BrokerAuthenticationError(
                    "production_token_identity_mismatch",
                    "Stored production token does not match the approved identity",
                )
            client = create_production_client(api_key)
            source = ProductionKiteInstrumentSource(client)
        else:
            client = create_sandbox_client(api_key)
            source = SandboxKiteInstrumentSource(client)
        client.set_access_token(token.access_token)
        now = SystemClock().now()
        snapshot = download_instruments(
            source,
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
    if production:
        typer.echo("Production market data used; production order routing remains disabled.")


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
    interval: Annotated[str, typer.Option("--interval", help="minute, 15minute, or day.")] = "day",
    root: Annotated[Path, typer.Option("--root", help="Historical data root.")] = Path("data"),
    token_path: Annotated[
        Path | None, typer.Option("--token-path", help="Session token file; defaults by mode.")
    ] = None,
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Use production historical market data; never enables order routing.",
        ),
    ] = False,
    calendar_path: Annotated[Path, typer.Option("--calendar", help="Market calendar YAML.")] = Path(
        "config/calendars/nse_equity_2026.yaml"
    ),
) -> None:
    """Download one idempotent, validated historical candle batch."""
    api_key_env = "KITE_API_KEY" if production else "KITE_SANDBOX_API_KEY"
    api_key = os.environ.get(api_key_env)
    if not api_key:
        code = "production_api_key_missing" if production else "sandbox_api_key_missing"
        _broker_failure(BrokerAuthenticationError(code, f"{api_key_env} is not configured"))
    selected_token_path = token_path or Path(
        "state/session/production_access_token.json"
        if production
        else "state/session/sandbox_access_token.json"
    )
    try:
        zone = ZoneInfo("Asia/Kolkata")
        start_at = datetime.combine(date.fromisoformat(start), time.min, zone)
        end_at = datetime.combine(date.fromisoformat(end), time.max, zone)
        stored = TokenStore(selected_token_path).load()
        if production:
            expected_user = os.environ.get("KITE_EXPECTED_USER_ID")
            if not expected_user:
                raise BrokerAuthenticationError(
                    "production_expected_user_missing",
                    "KITE_EXPECTED_USER_ID is not configured",
                )
            if stored.user_id != expected_user:
                raise BrokerAuthenticationError(
                    "production_token_identity_mismatch",
                    "Stored production token does not match the approved identity",
                )
            client = create_production_client(api_key)
        else:
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
    except BrokerAuthenticationError as error:
        _broker_failure(error)
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
    if production:
        typer.echo("Production historical data used; production order routing remains disabled.")


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


@app.command("kill-switch-on")
def kill_switch_on(
    reason: Annotated[str, typer.Option("--reason", help="Operator-visible activation reason.")],
    path: Annotated[Path, typer.Option("--path", help="SQLite database path.")] = Path(
        "state/trading.sqlite"
    ),
) -> None:
    """Persistently prevent new order approvals."""
    try:
        activated = KillSwitch(Database(path)).activate(reason, SystemClock().now())
    except (RiskError, StorageError) as error:
        _risk_failure(error)
    typer.echo("Kill switch activated." if activated else "Kill switch was already active.")


@app.command("kill-switch-status")
def kill_switch_status(
    path: Annotated[Path, typer.Option("--path", help="SQLite database path.")] = Path(
        "state/trading.sqlite"
    ),
) -> None:
    """Read persistent kill-switch state."""
    try:
        active = KillSwitch(Database(path)).active()
    except StorageError as error:
        _risk_failure(error)
    typer.echo("ACTIVE" if active else "INACTIVE")


@app.command("kill-switch-reset")
def kill_switch_reset(
    reconciled: Annotated[
        bool, typer.Option("--reconciled", help="Confirm reconciliation.")
    ] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Explicit human confirmation.")
    ] = False,
    path: Annotated[Path, typer.Option("--path", help="SQLite database path.")] = Path(
        "state/trading.sqlite"
    ),
) -> None:
    """Reset only after reconciliation and explicit human confirmation."""
    try:
        KillSwitch(Database(path)).reset(
            SystemClock().now(), reconciliation_healthy=reconciled, human_authorised=confirm
        )
    except (RiskError, StorageError) as error:
        _risk_failure(error)
    typer.echo("Kill switch reset.")


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


def _risk_failure(error: RiskError | StorageError) -> NoReturn:
    typer.echo(f"Risk error [{error.code}]: {error}", err=True)
    raise typer.Exit(code=1)
