"""Operator-controlled current-data runner whose only execution venue is PaperBroker."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time as time_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_quant.accounting import PortfolioAccounting
from personal_quant.broker.auth import TokenStore
from personal_quant.broker.contracts import BrokerProfile
from personal_quant.broker.production import create_production_client
from personal_quant.clocks import Clock, SystemClock
from personal_quant.costs import CostConfig, CostEngine, CostError
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.domain.money import Money
from personal_quant.instruments import InstrumentSnapshotStore
from personal_quant.live_data import (
    CollectorConfig,
    FeedState,
    KiteTickerClient,
    KiteTickerTransport,
    LiveDataCollector,
    LiveDataError,
    LiveTick,
    RawTick,
    RecordingResult,
    WebSocketMode,
    new_recorder,
)
from personal_quant.market_calendar import MarketCalendar
from personal_quant.oms import OrderManagementSystem
from personal_quant.paper import MarketBar, PaperBroker, PaperDeliveryCostEstimator
from personal_quant.paper_runtime import (
    DailyReport,
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

_INDIA = ZoneInfo("Asia/Kolkata")


class PaperRunnerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RunnerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(ge=1)
    evidence_kind: EvidenceKind
    database_path: Path
    runtime_config: Path
    risk_config: Path
    strategy_config: Path
    calendar_config: Path
    instrument_snapshot_directory: Path
    approved_instruments: tuple[InstrumentKey, ...] = Field(min_length=1)
    recording_root: Path
    token_path: Path
    startup_timeout_seconds: int = Field(gt=0, le=300)
    poll_interval_seconds: Decimal = Field(gt=0, le=5)
    bar_interval_seconds: int = Field(gt=0, le=300)

    @field_validator(
        "database_path",
        "runtime_config",
        "risk_config",
        "strategy_config",
        "calendar_config",
        "instrument_snapshot_directory",
        "recording_root",
        "token_path",
        mode="before",
    )
    @classmethod
    def paths(cls, value: object) -> object:
        return Path(value) if isinstance(value, str) else value

    @field_validator("evidence_kind", mode="before")
    @classmethod
    def evidence(cls, value: object) -> object:
        return EvidenceKind(value) if isinstance(value, str) else value

    @field_validator("approved_instruments", mode="before")
    @classmethod
    def instruments(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(InstrumentKey(str(item)) for item in value)
        return value

    @field_validator("poll_interval_seconds", mode="before")
    @classmethod
    def decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("poll_interval_seconds must be a quoted decimal string")
        return Decimal(value)

    @classmethod
    def load(cls, path: Path) -> RunnerConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise PaperRunnerError(
                "paper_runner_config_invalid", "Operational paper-runner config is invalid"
            ) from error


@dataclass(slots=True)
class BarAggregator:
    interval_seconds: int
    _bars: dict[InstrumentKey, tuple[int, Money, Money, Money, Money, int, int]] = field(
        default_factory=dict
    )

    def add(self, tick: LiveTick) -> MarketBar | None:
        bucket = int(tick.exchange_timestamp.timestamp()) // self.interval_seconds
        current = self._bars.get(tick.instrument)
        if current is None:
            self._bars[tick.instrument] = (
                bucket,
                tick.last_price,
                tick.last_price,
                tick.last_price,
                tick.last_price,
                tick.cumulative_volume,
                tick.cumulative_volume,
            )
            return None
        previous_bucket, opened, high, low, close, start_volume, last_volume = current
        if bucket == previous_bucket:
            self._bars[tick.instrument] = (
                bucket,
                opened,
                max(high, tick.last_price, key=lambda item: item.amount),
                min(low, tick.last_price, key=lambda item: item.amount),
                tick.last_price,
                start_volume,
                max(last_volume, tick.cumulative_volume),
            )
            return None
        completed = MarketBar(
            tick.instrument,
            datetime.fromtimestamp(previous_bucket * self.interval_seconds, tz=UTC)
            + timedelta(seconds=self.interval_seconds),
            opened,
            high,
            low,
            close,
            max(0, last_volume - start_volume),
        )
        self._bars[tick.instrument] = (
            bucket,
            tick.last_price,
            tick.last_price,
            tick.last_price,
            tick.last_price,
            tick.cumulative_volume,
            tick.cumulative_volume,
        )
        return completed


@dataclass(slots=True)
class OperationalPaperRunner:
    runtime: PaperRuntime
    collector: LiveDataCollector
    aggregator: BarAggregator
    clock: Clock
    startup_timeout_seconds: int
    poll_interval_seconds: float
    sleeper: Callable[[float], None] = time_module.sleep
    monotonic: Callable[[], float] = time_module.monotonic
    _stop_requested: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def on_tick(self, tick: LiveTick) -> None:
        with self._lock:
            bar = self.aggregator.add(tick)
            if bar is not None and self.runtime.state is RuntimeState.RUNNING:
                self.runtime.process_bar(bar)

    def start(self) -> None:
        self.collector.start()
        deadline = self.monotonic() + self.startup_timeout_seconds
        while not self.collector.health().healthy:
            if self.collector.state is FeedState.FAILED or self.monotonic() >= deadline:
                self.collector.stop()
                raise PaperRunnerError(
                    "paper_feed_not_ready", "Fresh current data did not pass the startup gate"
                )
            self.sleeper(self.poll_interval_seconds)
        preflight = self.runtime.preflight()
        if not preflight.passed:
            self.collector.stop()
            failed = ", ".join(name for name, passed in preflight.checks.items() if not passed)
            raise PaperRunnerError("paper_preflight_failed", f"Paper pre-flight failed: {failed}")
        self.runtime.start()

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_until_stopped(self) -> tuple[DailyReport, RecordingResult]:
        self.start()
        try:
            while not self._stop_requested:
                if self.collector.poll() is FeedState.FAILED:
                    raise PaperRunnerError("paper_feed_failed", "Current-data collector failed")
                self.sleeper(self.poll_interval_seconds)
        except BaseException:
            self.stop()
            raise
        return self.stop()

    def stop(self) -> tuple[DailyReport, RecordingResult]:
        with self._lock:
            recording = self.collector.stop()
            if self.runtime.state not in {RuntimeState.READY, RuntimeState.RUNNING}:
                raise PaperRunnerError(
                    "paper_runtime_not_active", "Paper runtime cannot shut down cleanly"
                )
            report = self.runtime.shutdown()
            return report, recording


class CallbackTicker(KiteTickerClient, Protocol):
    on_ticks: Callable[[object, Sequence[Mapping[str, object]]], None]
    on_connect: Callable[[object, object], None]
    on_close: Callable[[object, int, str], None]
    on_order_update: Callable[[object, Mapping[str, object]], None]


def bind_ticker_callbacks(ticker: CallbackTicker, collector: LiveDataCollector) -> None:
    ticker.on_connect = lambda _socket, _response: collector.on_connected()
    ticker.on_close = lambda _socket, code, reason: collector.on_disconnected(code, reason)
    ticker.on_order_update = lambda _socket, payload: collector.on_order_update(payload)

    def on_ticks(_socket: object, payloads: Sequence[Mapping[str, object]]) -> None:
        collector.on_ticks(tuple(parse_kite_tick(payload) for payload in payloads))

    ticker.on_ticks = on_ticks


def parse_kite_tick(payload: Mapping[str, object]) -> RawTick:
    try:
        exchange_timestamp = payload.get("exchange_timestamp") or payload["timestamp"]
        if not isinstance(exchange_timestamp, datetime):
            raise ValueError
        if exchange_timestamp.tzinfo is None:
            exchange_timestamp = exchange_timestamp.replace(tzinfo=_INDIA)
        broker_timestamp = payload.get("timestamp")
        if isinstance(broker_timestamp, datetime) and broker_timestamp.tzinfo is None:
            broker_timestamp = broker_timestamp.replace(tzinfo=_INDIA)
        depth = cast(Mapping[str, object], payload.get("depth") or {})
        buys = cast(Sequence[Mapping[str, object]], depth.get("buy") or ())
        sells = cast(Sequence[Mapping[str, object]], depth.get("sell") or ())
        bid = Money.from_value(str(buys[0]["price"])) if buys else None
        ask = Money.from_value(str(sells[0]["price"])) if sells else None
        return RawTick(
            InstrumentToken(int(str(payload["instrument_token"]))),
            exchange_timestamp,
            cast(datetime | None, broker_timestamp),
            Money.from_value(str(payload["last_price"])),
            bid,
            ask,
            int(str(payload.get("last_quantity") or 0)),
            int(str(payload.get("volume_traded") or payload.get("volume") or 0)),
        )
    except (KeyError, TypeError, ValueError):
        raise LiveDataError("kite_tick_invalid", "Kite tick payload is incomplete") from None


def build_operational_runner(config: RunnerConfig) -> OperationalPaperRunner:
    """Assemble live reads with PaperBroker; no production broker adapter is constructed."""
    clock = SystemClock()
    now = clock.now()
    token = TokenStore(config.token_path).load()
    api_key = os.environ.get("KITE_API_KEY", "")
    expected_user = os.environ.get("KITE_EXPECTED_USER_ID", "")
    if not api_key or not expected_user or token.user_id != expected_user:
        raise PaperRunnerError(
            "paper_auth_config_invalid", "Current-data authentication or expected user is invalid"
        )
    authenticated_at = datetime.fromisoformat(token.authenticated_at)
    auth_local = authenticated_at.astimezone(_INDIA)
    expiry = datetime.combine(auth_local.date(), time(6), _INDIA)
    if auth_local >= expiry:
        expiry += timedelta(days=1)
    if now >= expiry:
        raise PaperRunnerError("paper_token_expired", "Current-data token is not from today")

    profile_client = create_production_client(api_key)
    profile_client.set_access_token(token.access_token)
    raw_profile = profile_client.profile()
    profile = _profile(raw_profile)
    if profile.user_id != expected_user or profile.broker.upper() != "ZERODHA":
        raise PaperRunnerError("paper_account_mismatch", "Current-data account identity is wrong")

    snapshot = InstrumentSnapshotStore(config.instrument_snapshot_directory.parent.parent).load(
        config.instrument_snapshot_directory
    )
    local_date = now.astimezone(MarketCalendar.load(config.calendar_config).zone).date()
    if snapshot.manifest.snapshot_date != local_date:
        raise PaperRunnerError(
            "paper_instruments_stale", "Instrument snapshot is not for the current market date"
        )
    approved = {snapshot.resolve_token(key): key for key in config.approved_instruments}

    runtime_config = RuntimeConfig.load(config.runtime_config).model_copy(
        update={"evidence_kind": config.evidence_kind}
    )
    database = Database(config.database_path)
    MigrationRunner(database).apply_all()
    broker = PaperBroker(clock, opening_cash=Money(runtime_config.opening_cash_inr))
    accounting = PortfolioAccounting(database)
    broker.restore_portfolio(*_persisted_paper_portfolio(database, accounting, runtime_config))
    try:
        cost_estimator = PaperDeliveryCostEstimator(
            CostEngine(CostConfig.load(runtime_config.cost_config)),
            runtime_config.spread_bps,
            runtime_config.slippage_bps,
            runtime_config.impact_bps,
        )
    except CostError:
        raise PaperRunnerError(
            "paper_cost_config_invalid",
            "Versioned paper cost configuration cannot be loaded",
        ) from None
    oms = OrderManagementSystem(
        database,
        broker,
        clock,
        accounting,
        cost_estimator.estimate,
        broker.apply_cost,
    )
    strategy = PaperStrategyAdapter(
        StrategyRunner(
            BaselineMomentumStrategy(BaselineMomentumConfig.load(config.strategy_config)), "paper"
        )
    )
    collector_config = CollectorConfig(approved, mode=WebSocketMode.FULL)
    from kiteconnect import KiteTicker  # type: ignore[import-untyped]

    ticker = cast(CallbackTicker, KiteTicker(api_key, token.access_token))
    holder: dict[str, OperationalPaperRunner] = {}
    collector = LiveDataCollector(
        collector_config,
        KiteTickerTransport(ticker),
        new_recorder(config.recording_root, clock),
        clock,
        lambda tick: holder["runner"].on_tick(tick),
    )
    runtime = PaperRuntime(
        runtime_config,
        database,
        clock,
        MarketCalendar.load(config.calendar_config),
        collector.health,
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
    )
    runner = OperationalPaperRunner(
        runtime,
        collector,
        BarAggregator(config.bar_interval_seconds),
        clock,
        config.startup_timeout_seconds,
        float(config.poll_interval_seconds),
    )
    holder["runner"] = runner
    bind_ticker_callbacks(ticker, collector)
    return runner


def _profile(raw: Mapping[str, Any]) -> BrokerProfile:
    return BrokerProfile(
        str(raw["user_id"]),
        str(raw["user_name"]),
        str(raw["broker"]),
        tuple(str(item) for item in cast(Sequence[object], raw["exchanges"])),
        tuple(str(item) for item in cast(Sequence[object], raw["products"])),
    )


def _persisted_paper_portfolio(
    database: Database,
    accounting: PortfolioAccounting,
    runtime_config: RuntimeConfig,
) -> tuple[Money, dict[InstrumentKey, tuple[int, Money]]]:
    connection = database.connect(read_only=True)
    try:
        cash_entries = int(connection.execute("SELECT count(*) FROM cash_ledger").fetchone()[0])
        rows = connection.execute(
            "SELECT instrument_key, quantity, cost_basis_paise FROM positions WHERE quantity != 0"
        ).fetchall()
    finally:
        connection.close()
    cash = accounting.cash_balance() if cash_entries else Money(runtime_config.opening_cash_inr)
    positions = {
        InstrumentKey(str(row["instrument_key"])): (
            int(row["quantity"]),
            Money.from_value(
                Decimal(int(row["cost_basis_paise"])) / (Decimal(100) * int(row["quantity"]))
            ),
        )
        for row in rows
    }
    return cash, positions


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise PaperRunnerError("paper_git_unavailable", "Git commit cannot be verified") from None
