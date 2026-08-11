"""Deterministic historical market replay through the paper execution stack."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from personal_quant.accounting import PortfolioAccounting
from personal_quant.clocks import SimulatedClock
from personal_quant.costs import CostConfig, CostEngine, CostError
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.live_data import FeedState, HealthDecision
from personal_quant.market_calendar import MarketCalendar
from personal_quant.oms import OrderManagementSystem
from personal_quant.paper import MarketBar, PaperBroker, PaperDeliveryCostEstimator
from personal_quant.paper_runner import _file_hash, _git_commit, _persisted_paper_portfolio
from personal_quant.paper_runtime import (
    EvidenceKind,
    PaperRuntime,
    PreflightInputs,
    RuntimeConfig,
    RuntimeState,
)
from personal_quant.risk import RiskConfig, RiskEngine
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner
from personal_quant.strategy import BaselineMomentumConfig, BaselineMomentumStrategy, StrategyRunner
from personal_quant.strategy_adapters import PaperStrategyAdapter


class HistoricalPaperError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HistoricalPaperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int
    database_path: Path
    runtime_config: Path
    risk_config: Path
    strategy_config: Path
    calendar_config: Path
    historical_manifest: Path
    market_date: date
    instrument: InstrumentKey

    @field_validator(
        "database_path",
        "runtime_config",
        "risk_config",
        "strategy_config",
        "calendar_config",
        "historical_manifest",
        mode="before",
    )
    @classmethod
    def paths(cls, value: object) -> object:
        return Path(value) if isinstance(value, str) else value

    @field_validator("instrument", mode="before")
    @classmethod
    def instrument_key(cls, value: object) -> object:
        return InstrumentKey(value) if isinstance(value, str) else value

    @classmethod
    def load(cls, path: Path) -> HistoricalPaperConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise HistoricalPaperError(
                "historical_paper_config_invalid", "Historical paper configuration is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class HistoricalSourceBatch:
    manifest_path: Path
    checksum: str
    interval: str
    bars: tuple[MarketBar, ...]


@dataclass(frozen=True, slots=True)
class HistoricalPaperResult:
    session_id: str
    market_date: date
    bars: int
    report_path: Path
    source_checksum: str


def run_historical_paper_session(config: HistoricalPaperConfig) -> HistoricalPaperResult:
    """Replay one complete historical market date without live connectivity."""
    if config.database_path.resolve() == Path("state/trading.sqlite").resolve():
        raise HistoricalPaperError(
            "historical_paper_database_unsafe",
            "Historical replay cannot use the live operational database",
        )
    source = load_historical_source(config)
    database = Database(config.database_path)
    MigrationRunner(database).apply_all()
    _require_unused_date(database, config.market_date)

    clock = SimulatedClock(source.bars[0].timestamp)
    runtime_config = RuntimeConfig.load(config.runtime_config)
    if runtime_config.evidence_kind is not EvidenceKind.DRY:
        raise HistoricalPaperError(
            "historical_paper_evidence_invalid", "Historical replay must use dry evidence kind"
        )
    broker = PaperBroker(clock, opening_cash=Money(runtime_config.opening_cash_inr))
    accounting = PortfolioAccounting(database)
    broker.restore_portfolio(*_persisted_paper_portfolio(database, accounting, runtime_config))
    try:
        estimator = PaperDeliveryCostEstimator(
            CostEngine(CostConfig.load(runtime_config.cost_config)),
            runtime_config.spread_bps,
            runtime_config.slippage_bps,
            runtime_config.impact_bps,
        )
    except CostError:
        raise HistoricalPaperError(
            "historical_paper_cost_invalid", "Historical paper costs cannot be loaded"
        ) from None
    oms = OrderManagementSystem(
        database, broker, clock, accounting, estimator.estimate, broker.apply_cost
    )
    strategy = PaperStrategyAdapter(
        StrategyRunner(
            BaselineMomentumStrategy(BaselineMomentumConfig.load(config.strategy_config)),
            "replay",
        )
    )
    runtime = PaperRuntime(
        runtime_config,
        database,
        clock,
        MarketCalendar.load(config.calendar_config),
        lambda: HealthDecision(True, FeedState.HEALTHY, ()),
        strategy,
        RiskEngine(database, RiskConfig.load(config.risk_config)),
        oms,
        broker,
        accounting,
        PreflightInputs(
            _git_commit(),
            _file_hash(Path("uv.lock")),
            _file_hash(config.strategy_config),
            True,
            True,
            True,
        ),
        evidence_source="replay",
    )
    preflight = runtime.preflight()
    if not preflight.passed:
        failed = ", ".join(name for name, passed in preflight.checks.items() if not passed)
        raise HistoricalPaperError("historical_paper_preflight_failed", f"Failed: {failed}")
    runtime.start()
    for bar in source.bars:
        clock.set(bar.timestamp)
        runtime.process_bar(bar)
    report = runtime.shutdown()
    if runtime.state is not RuntimeState.STOPPED or not report.clean_shutdown:
        raise HistoricalPaperError(
            "historical_paper_shutdown_failed", "Historical paper session did not stop cleanly"
        )
    _record_replay(database, runtime, config, source)
    return HistoricalPaperResult(
        str(runtime.session_id),
        config.market_date,
        len(source.bars),
        runtime_config.report_root
        / report.ended_at.date().isoformat()
        / f"paper-session-{runtime.session_id}.json",
        source.checksum,
    )


def load_historical_source(config: HistoricalPaperConfig) -> HistoricalSourceBatch:
    try:
        manifest = json.loads(config.historical_manifest.read_text(encoding="utf-8"))
        request = manifest["request"]
        curated_path = Path(manifest["curated_path"])
        checksum = str(manifest["curated_checksum_sha256"])
        interval = str(request["interval"])
        start = datetime.fromisoformat(str(request["start"]))
        end = datetime.fromisoformat(str(request["end"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HistoricalPaperError(
            "historical_manifest_invalid", "Historical source manifest is invalid"
        ) from error
    if (
        manifest.get("status") != "complete"
        or int(manifest.get("invalid_rows", -1)) != 0
        or manifest.get("gaps") != []
        or interval not in {"minute", "15minute"}
        or start.date() != config.market_date
        or end.date() != config.market_date
        or str(request.get("instrument_key")) != str(config.instrument)
        or int(manifest.get("curated_rows", -1)) <= 0
    ):
        raise HistoricalPaperError(
            "historical_source_ineligible", "Replay requires one complete, gap-free market date"
        )
    if not curated_path.is_file() or _checksum(curated_path) != checksum:
        raise HistoricalPaperError(
            "historical_source_checksum", "Historical curated data checksum does not match"
        )
    try:
        # Read the immutable file directly. Dataset discovery would also infer Hive partition
        # columns from paths such as ``interval=minute`` and conflict with the file's interval
        # column type.
        records = pq.ParquetFile(curated_path).read().to_pylist()
        bars = tuple(
            MarketBar(
                InstrumentKey(str(row["instrument_key"])),
                row["timestamp"].astimezone(UTC),
                Money.from_value(str(row["open"])),
                Money.from_value(str(row["high"])),
                Money.from_value(str(row["low"])),
                Money.from_value(str(row["close"])),
                int(row["volume"]),
            )
            for row in records
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HistoricalPaperError(
            "historical_source_rows_invalid", "Historical curated rows are invalid"
        ) from error
    calendar = MarketCalendar.load(config.calendar_config)
    if (
        not bars
        or len(bars) != int(manifest["curated_rows"])
        or tuple(sorted(bars, key=lambda item: item.timestamp)) != bars
        or len({bar.timestamp for bar in bars}) != len(bars)
        or any(bar.timestamp.astimezone(calendar.zone).date() != config.market_date for bar in bars)
    ):
        raise HistoricalPaperError(
            "historical_source_order_invalid", "Historical bars must be non-empty and ordered"
        )
    if any(bar.instrument != config.instrument for bar in bars):
        raise HistoricalPaperError(
            "historical_source_instrument", "Historical bars contain an unexpected instrument"
        )
    return HistoricalSourceBatch(config.historical_manifest, checksum, interval, bars)


def _require_unused_date(database: Database, market_date: date) -> None:
    connection = database.connect(read_only=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM historical_paper_sessions WHERE market_date=?",
            (market_date.isoformat(),),
        ).fetchone()
        latest = connection.execute(
            "SELECT max(market_date) FROM historical_paper_sessions"
        ).fetchone()[0]
    finally:
        connection.close()
    if exists is not None:
        raise HistoricalPaperError(
            "historical_paper_date_duplicate", "Only one replay session may count per market date"
        )
    if latest is not None and market_date.isoformat() <= str(latest):
        raise HistoricalPaperError(
            "historical_paper_date_order",
            "Replay dates must be processed in strictly increasing order",
        )


def _record_replay(
    database: Database,
    runtime: PaperRuntime,
    config: HistoricalPaperConfig,
    source: HistoricalSourceBatch,
) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """
            INSERT INTO historical_paper_sessions VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                str(runtime.session_id),
                config.market_date.isoformat(),
                str(config.instrument),
                source.interval,
                str(source.manifest_path),
                source.checksum,
                len(source.bars),
                runtime.clock.now().astimezone(UTC).isoformat(),
            ),
        )


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
