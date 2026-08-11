"""Fail-closed paper runtime foundation with persisted lifecycle evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_quant.accounting import JournalType, PortfolioAccounting
from personal_quant.broker.contracts import OrderSide
from personal_quant.clocks import Clock
from personal_quant.config import OperatingMode
from personal_quant.domain.identifiers import InstrumentKey, StrategyId
from personal_quant.domain.money import Money
from personal_quant.live_data import HealthDecision
from personal_quant.market_calendar import MarketCalendar
from personal_quant.oms import OmsIntent, OrderManagementSystem, OrderState, StoredOrder
from personal_quant.paper import MarketBar, PaperBroker
from personal_quant.risk import (
    KillSwitch,
    OrderIntent,
    RiskContext,
    RiskDecisionType,
    RiskEngine,
)
from personal_quant.storage.database import Database
from personal_quant.strategy import Signal
from personal_quant.strategy_adapters import PaperStrategyAdapter, desired_side

RUNTIME_NAMESPACE = UUID("050b49a8-47c3-43ea-aebc-f9b170a35984")


class RuntimeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RuntimeState(StrEnum):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    READY = "READY"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class EvidenceKind(StrEnum):
    DRY = "dry"
    FORMAL = "formal"


class SchedulePhase(StrEnum):
    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    MARKET = "MARKET"
    STRATEGY = "STRATEGY"
    STOP_NEW_ENTRIES = "STOP_NEW_ENTRIES"
    POST_CLOSE = "POST_CLOSE"


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(ge=1)
    mode: str = Field(pattern="^paper$")
    evidence_kind: EvidenceKind
    minimum_free_disk_mb: int = Field(gt=0)
    opening_cash_inr: Decimal = Field(gt=0)
    tick_size_inr: Decimal = Field(gt=0)
    expected_gross_edge_inr: Decimal = Field(gt=0)
    expected_costs_inr: Decimal = Field(ge=0)
    cost_config: Path
    spread_bps: Decimal = Field(ge=0)
    slippage_bps: Decimal = Field(ge=0)
    impact_bps: Decimal = Field(ge=0)
    maximum_price_deviation_pct: Decimal = Field(gt=0)
    stop_loss_pct: Decimal = Field(gt=0, lt=100)
    cancel_open_orders_on_shutdown: bool
    report_root: Path
    lock_path: Path

    @field_validator("evidence_kind", mode="before")
    @classmethod
    def evidence(cls, value: object) -> object:
        return EvidenceKind(value) if isinstance(value, str) else value

    @field_validator("report_root", "lock_path", "cost_config", mode="before")
    @classmethod
    def paths(cls, value: object) -> object:
        return Path(value) if isinstance(value, str) else value

    @field_validator(
        "opening_cash_inr",
        "tick_size_inr",
        "expected_gross_edge_inr",
        "expected_costs_inr",
        "spread_bps",
        "slippage_bps",
        "impact_bps",
        "maximum_price_deviation_pct",
        "stop_loss_pct",
        mode="before",
    )
    @classmethod
    def decimals(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("runtime decimals must be quoted strings")
        return Decimal(value)

    @classmethod
    def load(cls, path: Path) -> RuntimeConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise RuntimeError(
                "runtime_config_invalid", "Paper runtime configuration is invalid"
            ) from error

    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(slots=True)
class ProcessLock:
    path: Path
    session_id: UUID
    started_at: datetime
    _held: bool = False

    def acquire(self) -> None:
        _aware(self.started_at)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "session_id": str(self.session_id),
            "started_at": self.started_at.astimezone(UTC).isoformat(),
        }
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = self._read()
            pid = int(str(existing.get("pid", -1)))
            if _pid_running(pid):
                raise RuntimeError(
                    "runtime_already_running", "Another runtime owns the lock"
                ) from None
            self.path.unlink()
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        existing = self._read()
        if existing.get("session_id") != str(self.session_id):
            raise RuntimeError("runtime_lock_changed", "Runtime lock ownership changed")
        self.path.unlink(missing_ok=True)
        self._held = False

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("runtime_lock_invalid", "Runtime lock is malformed") from error


@dataclass(frozen=True, slots=True)
class PreflightInputs:
    git_commit: str
    release_manifest_hash: str
    strategy_manifest_hash: str
    authentication_valid: bool
    account_matches: bool
    instrument_master_current: bool


@dataclass(frozen=True, slots=True)
class PreflightReport:
    passed: bool
    checks: dict[str, bool]
    interrupted_sessions: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class RuntimeProgress:
    successful_dry_sessions: int
    successful_formal_sessions: int
    dry_requirement_met: bool
    formal_requirement_met: bool


@dataclass(frozen=True, slots=True)
class DailyReport:
    session_id: UUID
    started_at: datetime
    ended_at: datetime
    runtime_state: RuntimeState
    signals: int
    risk_approved: int
    risk_rejected: int
    orders_submitted: int
    fills: int
    open_orders: int
    cash: Money
    market_value: Money
    net_liquidation_value: Money
    realised_pnl: Money
    unrealised_pnl: Money
    variable_costs: Money
    trading_net_pnl: Money
    positions: dict[str, int]
    reconciliation_healthy: bool
    clean_shutdown: bool


@dataclass(slots=True)
class PaperRuntime:
    config: RuntimeConfig
    database: Database
    clock: Clock
    calendar: MarketCalendar
    feed_health: Callable[[], HealthDecision]
    strategy: PaperStrategyAdapter
    risk: RiskEngine
    oms: OrderManagementSystem
    broker: PaperBroker
    accounting: PortfolioAccounting
    preflight_inputs: PreflightInputs
    evidence_source: str = "live"
    session_id: UUID = field(default_factory=uuid4)
    state: RuntimeState = RuntimeState.CREATED
    reconciliation_healthy: bool = False
    _lock: ProcessLock | None = None
    _started_at: datetime | None = None
    _signals: int = 0
    _risk_approved: int = 0
    _risk_rejected: int = 0
    _orders_submitted: int = 0
    _last_prices: dict[InstrumentKey, Money] = field(default_factory=dict)
    _starting_realised_paise: int = 0
    _starting_costs: Money = field(default_factory=lambda: Money.from_value(0))

    def preflight(self) -> PreflightReport:
        if self.state is not RuntimeState.CREATED:
            raise RuntimeError("preflight_state_invalid", "Pre-flight can run only once")
        now = self.clock.now()
        _aware(now)
        if self.evidence_source not in {"live", "replay"}:
            raise RuntimeError("evidence_source_invalid", "Runtime evidence source is invalid")
        if self.config.evidence_kind is EvidenceKind.FORMAL:
            progress = runtime_progress(self.database)
            if not progress.dry_requirement_met:
                raise RuntimeError(
                    "formal_sessions_locked", "Ten successful dry sessions are required first"
                )
        self._lock = ProcessLock(self.config.lock_path, self.session_id, now)
        self._lock.acquire()
        self._started_at = now
        self.state = RuntimeState.PREFLIGHT
        interrupted = self._mark_interrupted(now)
        self._initialize_cash(now)
        self._starting_realised_paise, self._starting_costs = self._accounting_totals()
        differences = self._reconcile()
        feed = self._feed_decision()
        free_mb = shutil.disk_usage(self.database.path.parent).free // (1024 * 1024)
        checks = {
            "git_commit": len(self.preflight_inputs.git_commit) >= 7,
            "release_manifest": len(self.preflight_inputs.release_manifest_hash) == 64,
            "strategy_manifest": len(self.preflight_inputs.strategy_manifest_hash) == 64,
            "clock": True,
            "disk": free_mb >= self.config.minimum_free_disk_mb,
            "database": self.database.integrity_check().passed,
            "authentication": self.preflight_inputs.authentication_valid,
            "account": self.preflight_inputs.account_matches,
            "instrument_master": self.preflight_inputs.instrument_master_current,
            "calendar": self.calendar.session_times(now.astimezone(self.calendar.zone).date())
            is not None,
            "feed": feed.healthy,
            "kill_switch": not KillSwitch(self.database).active(),
            "reconciliation": not differences,
            "open_order_recovery": all(
                order.state is not OrderState.RECONCILIATION_REQUIRED
                for order in self.oms.open_orders()
            ),
        }
        self.reconciliation_healthy = checks["reconciliation"] and checks["open_order_recovery"]
        self._insert_session(now)
        if not all(checks.values()):
            self.state = RuntimeState.FAILED
            self._update_session(ended_at=now, failure_code="preflight_failed")
            self._lock.release()
            return PreflightReport(False, checks, interrupted)
        self.state = RuntimeState.READY
        self._update_session(ready_at=now)
        self._snapshot("preflight")
        return PreflightReport(True, checks, interrupted)

    def start(self) -> None:
        if self.state is not RuntimeState.READY:
            raise RuntimeError("runtime_not_ready", "Runtime cannot start before pre-flight")
        self.state = RuntimeState.RUNNING
        self._update_session()

    def process_bar(self, bar: MarketBar) -> tuple[StoredOrder, ...]:
        if self.state is not RuntimeState.RUNNING:
            raise RuntimeError("runtime_not_running", "Runtime is not accepting market events")
        if schedule_phase(self.calendar, bar.timestamp) is not SchedulePhase.STRATEGY:
            return ()
        self._last_prices[bar.instrument] = bar.close
        feed = self._feed_decision()
        if (
            not feed.healthy
            or KillSwitch(self.database).active()
            or not self.reconciliation_healthy
        ):
            return ()
        existing = self.oms.open_orders()
        self.broker.process_bar(bar)
        for order in existing:
            self.oms.sync(order.order_id)
        positions = {item.instrument: item.quantity for item in self.broker.get_positions()}
        averages = {item.instrument: item.average_price for item in self.broker.get_positions()}
        signals = self.strategy.on_bar(
            bar,
            cash=self.broker.get_funds().available_cash,
            positions=positions,
            average_entry=averages,
        )
        created: list[StoredOrder] = []
        for signal in signals:
            self._signals += 1
            handled = self._handle_signal(signal, bar, positions)
            if handled is not None:
                created.append(handled)
        self._snapshot("bar")
        return tuple(created)

    def shutdown(self) -> DailyReport:
        if self.state not in {RuntimeState.READY, RuntimeState.RUNNING}:
            raise RuntimeError("shutdown_state_invalid", "Runtime is not active")
        self.state = RuntimeState.STOPPING
        self._update_session()
        if self.config.cancel_open_orders_on_shutdown:
            for order in self.oms.open_orders():
                if order.state in {OrderState.OPEN, OrderState.PARTIALLY_FILLED}:
                    self.oms.cancel(order.order_id)
        differences = self._reconcile()
        self.reconciliation_healthy = not differences
        eligible = self.reconciliation_healthy and not KillSwitch(self.database).active()
        self._snapshot("shutdown")
        now = self.clock.now()
        report = self._daily_report(now, clean=eligible)
        path = self._write_report(report)
        self.state = RuntimeState.STOPPED if eligible else RuntimeState.FAILED
        self._update_session(
            ended_at=now,
            clean_shutdown=eligible,
            report_path=path,
            failure_code=None if eligible else "shutdown_safety_gate",
        )
        if self._lock is not None:
            self._lock.release()
        return report

    def _handle_signal(
        self, signal: Signal, bar: MarketBar, positions: dict[InstrumentKey, int]
    ) -> StoredOrder | None:
        current = positions.get(signal.instrument, 0)
        side = desired_side(signal, current)
        if side is None:
            return None
        quantity = abs(signal.target_position - current)
        key = f"{signal.signal_id}:{side.value}:{quantity}"
        limit = _round_tick(bar.close, Money(self.config.tick_size_inr))
        stop = (
            Money(limit.amount * (Decimal(1) - self.config.stop_loss_pct / 100))
            if side is OrderSide.BUY
            else limit
        )
        intent = OrderIntent(
            signal.signal_id,
            key,
            StrategyId(signal.strategy_id),
            signal.instrument,
            side,
            quantity,
            limit,
            stop,
            Money(self.config.tick_size_inr),
            Money(self.config.expected_gross_edge_inr),
            Money(self.config.expected_costs_inr),
            signal.expires_at,
        )
        decision = self.risk.evaluate(intent, self._risk_context(intent, bar))
        if decision.decision is RiskDecisionType.REJECTED:
            self._risk_rejected += 1
        else:
            self._risk_approved += 1
        oms_intent = OmsIntent(
            uuid5(RUNTIME_NAMESPACE, key),
            intent.intent_id,
            intent.idempotency_key,
            intent.instrument_key,
            intent.side,
            intent.quantity,
            intent.limit_price,
        )
        stored = self.oms.register(oms_intent, decision.decision_id)
        if stored.state is OrderState.RISK_APPROVED:
            self._orders_submitted += 1
            return self.oms.submit(stored.order_id)
        return stored

    def _risk_context(self, intent: OrderIntent, bar: MarketBar) -> RiskContext:
        broker_positions = self.broker.get_positions()
        current = next(
            (item for item in broker_positions if item.instrument == intent.instrument_key), None
        )
        current_quantity = current.quantity if current else 0
        current_exposure = (
            current.average_price.multiply(current_quantity) if current else Money.from_value(0)
        )
        gross = Money(
            sum(
                (item.average_price.amount * item.quantity for item in broker_positions), Decimal(0)
            )
        )
        return RiskContext(
            self.clock.now(),
            OperatingMode.PAPER,
            False,
            self.preflight_inputs.authentication_valid,
            self.preflight_inputs.account_matches,
            False,
            "NSE",
            "CNC",
            True,
            True,
            bar.timestamp,
            Decimal(0),
            Money(bar.close.amount - self.config.tick_size_inr),
            Money(bar.close.amount + self.config.tick_size_inr),
            self.calendar.is_strategy_window(bar.timestamp),
            True,
            bar.close,
            self.config.maximum_price_deviation_pct,
            len(broker_positions),
            current_quantity,
            current_exposure,
            gross,
            self.broker.get_funds().available_cash,
            Money.from_value(0),
            Money.from_value(0),
            Decimal(0),
            self._orders_submitted,
            0,
            0,
            self.reconciliation_healthy,
            False,
        )

    def _initialize_cash(self, now: datetime) -> None:
        with self.database.transaction(write=False) as connection:
            count = int(connection.execute("SELECT count(*) FROM cash_ledger").fetchone()[0])
        if count == 0:
            self.accounting.append_journal(
                entry_id=uuid5(RUNTIME_NAMESPACE, "paper-opening-cash"),
                entry_type=JournalType.OPENING_CASH,
                amount=Money(self.config.opening_cash_inr),
                occurred_at=now,
                note="Paper runtime opening cash",
            )

    def _reconcile(self) -> tuple[object, ...]:
        position_snapshot = {
            item.instrument: (item.quantity, item.average_price)
            for item in self.broker.get_positions()
        }
        return (
            *self.accounting.reconcile_cash(self.broker.get_funds().available_cash),
            *self.accounting.reconcile_positions(position_snapshot),
        )

    def _feed_decision(self) -> HealthDecision:
        decision = self.feed_health()
        if not isinstance(decision, HealthDecision):
            raise RuntimeError("feed_health_invalid", "Feed health provider returned invalid data")
        return decision

    def _mark_interrupted(self, now: datetime) -> tuple[UUID, ...]:
        with self.database.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT session_id FROM paper_runtime_sessions WHERE ended_at IS NULL"
            ).fetchall()
            connection.execute(
                """
                UPDATE paper_runtime_sessions SET state='INTERRUPTED', ended_at=?,
                    failure_code='unclean_previous_session'
                WHERE ended_at IS NULL
                """,
                (_iso(now),),
            )
        return tuple(UUID(row["session_id"]) for row in rows)

    def _insert_session(self, now: datetime) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO paper_runtime_sessions(
                    session_id, evidence_kind, state, started_at, git_commit,
                    release_manifest_hash, strategy_manifest_hash, config_hash, evidence_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(self.session_id),
                    self.config.evidence_kind.value,
                    self.state.value,
                    _iso(now),
                    self.preflight_inputs.git_commit,
                    self.preflight_inputs.release_manifest_hash,
                    self.preflight_inputs.strategy_manifest_hash,
                    self.config.fingerprint(),
                    self.evidence_source,
                ),
            )

    def _update_session(
        self,
        *,
        ready_at: datetime | None = None,
        ended_at: datetime | None = None,
        clean_shutdown: bool | None = None,
        report_path: Path | None = None,
        failure_code: str | None = None,
    ) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """
                UPDATE paper_runtime_sessions SET state=?,
                    ready_at=COALESCE(?, ready_at), ended_at=COALESCE(?, ended_at),
                    clean_shutdown=COALESCE(?, clean_shutdown),
                    reconciliation_healthy=?, report_path=COALESCE(?, report_path),
                    failure_code=? WHERE session_id=?
                """,
                (
                    self.state.value,
                    _iso(ready_at) if ready_at else None,
                    _iso(ended_at) if ended_at else None,
                    int(clean_shutdown) if clean_shutdown is not None else None,
                    int(self.reconciliation_healthy),
                    str(report_path) if report_path else None,
                    failure_code,
                    str(self.session_id),
                ),
            )

    def _snapshot(self, kind: str) -> None:
        payload = {
            "state": self.state.value,
            "feed": asdict(self._feed_decision()),
            "kill_switch": KillSwitch(self.database).active(),
            "cash": str(self.broker.get_funds().available_cash.amount),
            "positions": [asdict(item) for item in self.broker.get_positions()],
            "orders": [asdict(item) for item in self.oms.open_orders()],
            "reconciliation_healthy": self.reconciliation_healthy,
        }
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO runtime_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(self.session_id),
                    kind,
                    json.dumps(payload, default=_json_default, sort_keys=True),
                    _iso(self.clock.now()),
                ),
            )

    def _daily_report(self, ended_at: datetime, *, clean: bool) -> DailyReport:
        broker_positions = self.broker.get_positions()
        positions = {str(item.instrument): item.quantity for item in broker_positions}
        market_value = Money(
            sum(
                (
                    self._last_prices.get(item.instrument, item.average_price).amount
                    * item.quantity
                    for item in broker_positions
                ),
                Decimal(0),
            )
        )
        unrealised = Money(
            sum(
                (
                    (
                        self._last_prices.get(item.instrument, item.average_price).amount
                        - item.average_price.amount
                    )
                    * item.quantity
                    for item in broker_positions
                ),
                Decimal(0),
            )
        )
        realised_paise, total_costs = self._accounting_totals()
        realised = Money(Decimal(realised_paise - self._starting_realised_paise) / 100)
        costs = total_costs - self._starting_costs
        cash = self.broker.get_funds().available_cash
        return DailyReport(
            self.session_id,
            self._started_at or ended_at,
            ended_at,
            RuntimeState.STOPPED if clean else RuntimeState.FAILED,
            self._signals,
            self._risk_approved,
            self._risk_rejected,
            self._orders_submitted,
            len(self.broker.get_trades()),
            len(self.oms.open_orders()),
            cash,
            market_value,
            cash + market_value,
            realised,
            unrealised,
            costs,
            realised + unrealised - costs,
            positions,
            self.reconciliation_healthy,
            clean,
        )

    def _accounting_totals(self) -> tuple[int, Money]:
        connection = self.database.connect(read_only=True)
        try:
            realised_paise = int(
                connection.execute(
                    "SELECT COALESCE(SUM(realised_pnl_paise), 0) FROM positions"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        return realised_paise, self.accounting.cost_total()

    def _write_report(self, report: DailyReport) -> Path:
        path = (
            self.config.report_root
            / report.ended_at.date().isoformat()
            / f"paper-session-{self.session_id}.json"
        )
        if path.exists():
            raise RuntimeError("report_exists", "Daily report is immutable")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(report), default=_json_default, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return path


def schedule_phase(calendar: MarketCalendar, moment: datetime) -> SchedulePhase:
    _aware(moment)
    local = moment.astimezone(calendar.zone)
    session = calendar.session_times(local.date())
    if (
        session is None
        or local.time() < session.pre_open_start
        or local.time() > session.post_close_end
    ):
        return SchedulePhase.CLOSED
    if local.time() < session.market_open:
        return SchedulePhase.PRE_MARKET
    if local.time() < session.strategy_start:
        return SchedulePhase.MARKET
    if local.time() <= session.strategy_end:
        return SchedulePhase.STRATEGY
    if local.time() <= session.market_close:
        return SchedulePhase.STOP_NEW_ENTRIES
    return SchedulePhase.POST_CLOSE


def runtime_progress(database: Database) -> RuntimeProgress:
    connection = database.connect(read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT evidence_kind, count(*) AS count FROM paper_runtime_sessions
            WHERE evidence_source='live' AND state='STOPPED'
                AND clean_shutdown=1 AND reconciliation_healthy=1
            GROUP BY evidence_kind
            """
        ).fetchall()
    finally:
        connection.close()
    counts = {str(row["evidence_kind"]): int(row["count"]) for row in rows}
    dry, formal = counts.get("dry", 0), counts.get("formal", 0)
    return RuntimeProgress(dry, formal, dry >= 10, formal >= 30)


def _round_tick(value: Money, tick: Money) -> Money:
    units = (value.amount / tick.amount).to_integral_value(rounding=ROUND_FLOOR)
    return Money(units * tick.amount)


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _json_default(value: object) -> object:
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency.value}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (UUID, StrEnum, Decimal)):
        return str(value)
    return value


def _iso(value: datetime) -> str:
    _aware(value)
    return value.astimezone(UTC).isoformat()


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("runtime_time_naive", "Runtime timestamps must be timezone-aware")
