from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from personal_quant.accounting import PortfolioAccounting
from personal_quant.clocks import SimulatedClock
from personal_quant.costs import CostConfig, CostEngine
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.live_data import FeedState, HealthDecision
from personal_quant.market_calendar import MarketCalendar
from personal_quant.oms import OrderManagementSystem, OrderState, StoredOrder
from personal_quant.paper import MarketBar, PaperBroker, PaperDeliveryCostEstimator
from personal_quant.paper_runtime import (
    EvidenceKind,
    PaperRuntime,
    PreflightInputs,
    ProcessLock,
    RuntimeConfig,
    RuntimeError,
    RuntimeState,
    SchedulePhase,
    runtime_progress,
    schedule_phase,
)
from personal_quant.risk import RiskConfig, RiskEngine
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner
from personal_quant.strategy import (
    BaselineMomentumConfig,
    BaselineMomentumStrategy,
    Signal,
    SignalDirection,
    SignalPurpose,
    StrategyRunner,
)
from personal_quant.strategy_adapters import PaperStrategyAdapter

NOW = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)  # 09:30 Asia/Kolkata
INSTRUMENT = InstrumentKey("NSE:INFY")
RUNTIME_CONFIG = Path("config/paper_runtime.example.yaml")
RISK_CONFIG = Path("config/risk/conservative_10k.yaml")
STRATEGY_CONFIG = Path("config/strategies/baseline_momentum_v1.yaml")
CALENDAR_CONFIG = Path("config/calendars/nse_equity_2026.yaml")


class Feed:
    def __init__(self) -> None:
        self.decision = HealthDecision(True, FeedState.HEALTHY, ())

    def __call__(self) -> HealthDecision:
        return self.decision


def assert_runtime_state(runtime: PaperRuntime, expected: RuntimeState) -> None:
    assert runtime.state is expected


def make_runtime(
    tmp_path: Path,
    *,
    database: Database | None = None,
    feed: Feed | None = None,
    evidence: EvidenceKind = EvidenceKind.DRY,
    lock_name: str = "runtime.lock",
) -> tuple[PaperRuntime, SimulatedClock, Feed]:
    db = database or Database(tmp_path / "trading.sqlite")
    MigrationRunner(db).apply_all()
    clock = SimulatedClock(NOW)
    config = RuntimeConfig.load(RUNTIME_CONFIG).model_copy(
        update={
            "evidence_kind": evidence,
            "minimum_free_disk_mb": 1,
            "report_root": tmp_path / "reports",
            "lock_path": tmp_path / lock_name,
        }
    )
    broker = PaperBroker(clock)
    accounting = PortfolioAccounting(db)
    estimator = PaperDeliveryCostEstimator(
        CostEngine(CostConfig.load(config.cost_config)),
        config.spread_bps,
        config.slippage_bps,
        config.impact_bps,
    )
    oms = OrderManagementSystem(
        db,
        broker,
        clock,
        accounting,
        estimator.estimate,
        broker.apply_cost,
    )
    selected_feed = feed or Feed()
    strategy = PaperStrategyAdapter(
        StrategyRunner(
            BaselineMomentumStrategy(BaselineMomentumConfig.load(STRATEGY_CONFIG)),
            "paper",
        )
    )
    runtime = PaperRuntime(
        config,
        db,
        clock,
        MarketCalendar.load(CALENDAR_CONFIG),
        selected_feed,
        strategy,
        RiskEngine(db, RiskConfig.load(RISK_CONFIG)),
        oms,
        broker,
        accounting,
        PreflightInputs("a" * 40, "b" * 64, "c" * 64, True, True, True),
    )
    return runtime, clock, selected_feed


def market_bar(clock: SimulatedClock, index: int) -> MarketBar:
    close = Decimal(100 + index)
    return MarketBar(
        INSTRUMENT,
        clock.now(),
        Money(close),
        Money(close + 1),
        Money(close - (3 if index == 5 else 1)),
        Money(close),
        20_000,
    )


def test_full_runtime_lifecycle_executes_paper_fill_and_writes_evidence(tmp_path: Path) -> None:
    runtime, clock, _ = make_runtime(tmp_path)
    preflight = runtime.preflight()
    assert preflight.passed
    assert_runtime_state(runtime, RuntimeState.READY)
    runtime.start()

    created: tuple[StoredOrder, ...] = ()
    for index in range(5):
        created = runtime.process_bar(market_bar(clock, index))
        clock.advance(timedelta(minutes=1))
    assert len(created) == 1
    assert created[0].state is OrderState.OPEN
    runtime.process_bar(market_bar(clock, 5))
    assert runtime.broker.get_trades()
    position = runtime.accounting.position(INSTRUMENT)
    assert position is not None and position.quantity == 10
    assert runtime.accounting.cost_total().amount > 0
    assert runtime.accounting.cash_balance() == runtime.broker.get_funds().available_cash

    report = runtime.shutdown()
    assert report.clean_shutdown
    assert report.reconciliation_healthy
    assert report.signals == 1
    assert report.risk_approved == 1
    assert report.orders_submitted == 1
    assert report.fills == 1
    assert report.variable_costs.amount > 0
    assert_runtime_state(runtime, RuntimeState.STOPPED)
    assert runtime_progress(runtime.database).successful_dry_sessions == 1
    connection = runtime.database.connect(read_only=True)
    try:
        snapshots = connection.execute(
            "SELECT snapshot_kind FROM runtime_snapshots WHERE session_id=?",
            (str(runtime.session_id),),
        ).fetchall()
        report_path = connection.execute(
            "SELECT report_path FROM paper_runtime_sessions WHERE session_id=?",
            (str(runtime.session_id),),
        ).fetchone()[0]
    finally:
        connection.close()
    assert {row[0] for row in snapshots} >= {"preflight", "bar", "shutdown"}
    assert Path(report_path).is_file()
    assert not runtime.config.lock_path.exists()


def test_stale_feed_kill_switch_and_schedule_block_new_signals(tmp_path: Path) -> None:
    runtime, clock, feed = make_runtime(tmp_path)
    assert runtime.preflight().passed
    runtime.start()
    feed.decision = HealthDecision(False, FeedState.STALE, ("feed_stale",))
    for index in range(6):
        assert runtime.process_bar(market_bar(clock, index)) == ()
        clock.advance(timedelta(minutes=1))
    assert runtime.broker.get_orders() == ()
    feed.decision = HealthDecision(True, FeedState.HEALTHY, ())
    from personal_quant.risk import KillSwitch

    KillSwitch(runtime.database).activate("test stop", clock.now())
    assert runtime.process_bar(market_bar(clock, 6)) == ()
    runtime.shutdown()


def test_runtime_allows_only_one_active_order_per_instrument(tmp_path: Path) -> None:
    runtime, clock, _ = make_runtime(tmp_path)
    assert runtime.preflight().passed
    runtime.start()
    bar = market_bar(clock, 0)

    def signal() -> Signal:
        return Signal(
            uuid4(),
            "test-strategy",
            "1.0.0",
            INSTRUMENT,
            clock.now(),
            SignalDirection.LONG,
            Decimal("1"),
            10,
            5,
            "test invalidation",
            ("test",),
            {},
            "test-model",
            clock.now() + timedelta(minutes=5),
            SignalPurpose.ENTRY,
        )

    first = runtime._handle_signal(signal(), bar, {})
    second = runtime._handle_signal(signal(), bar, {})

    assert first is not None and first.state is OrderState.OPEN
    assert second is None
    assert len(runtime.broker.get_orders()) == 1
    runtime.shutdown()


def test_preflight_failure_releases_lock_and_formal_sessions_are_gated(tmp_path: Path) -> None:
    feed = Feed()
    feed.decision = HealthDecision(False, FeedState.STALE, ("feed_stale",))
    runtime, _, _ = make_runtime(tmp_path, feed=feed)
    report = runtime.preflight()
    assert not report.passed
    assert not report.checks["feed"]
    assert runtime.state is RuntimeState.FAILED
    assert not runtime.config.lock_path.exists()

    formal, _, _ = make_runtime(
        tmp_path, database=runtime.database, evidence=EvidenceKind.FORMAL, lock_name="formal.lock"
    )
    with pytest.raises(RuntimeError) as error:
        formal.preflight()
    assert error.value.code == "formal_sessions_locked"


def test_unclean_session_is_marked_interrupted_before_recovery(tmp_path: Path) -> None:
    first, _, _ = make_runtime(tmp_path, lock_name="first.lock")
    assert first.preflight().passed
    first.start()
    assert first._lock is not None
    first._lock.release()

    second, _, _ = make_runtime(tmp_path, database=first.database, lock_name="second.lock")
    report = second.preflight()
    assert report.passed
    assert report.interrupted_sessions == (first.session_id,)
    connection = first.database.connect(read_only=True)
    try:
        state = connection.execute(
            "SELECT state FROM paper_runtime_sessions WHERE session_id=?", (str(first.session_id),)
        ).fetchone()[0]
    finally:
        connection.close()
    assert state == "INTERRUPTED"
    second.shutdown()


def test_scheduler_process_lock_and_progress_rules(tmp_path: Path) -> None:
    calendar = MarketCalendar.load(CALENDAR_CONFIG)

    def at(local_time: time) -> datetime:
        return datetime.combine(NOW.astimezone(calendar.zone).date(), local_time, calendar.zone)

    assert schedule_phase(calendar, at(time(8, 59))) is SchedulePhase.CLOSED
    assert schedule_phase(calendar, at(time(9, 5))) is SchedulePhase.PRE_MARKET
    assert schedule_phase(calendar, at(time(9, 16))) is SchedulePhase.MARKET
    assert schedule_phase(calendar, at(time(10))) is SchedulePhase.STRATEGY
    assert schedule_phase(calendar, at(time(15, 20))) is SchedulePhase.STOP_NEW_ENTRIES
    assert schedule_phase(calendar, at(time(15, 45))) is SchedulePhase.POST_CLOSE

    lock = ProcessLock(tmp_path / "single.lock", uuid4(), NOW)
    lock.acquire()
    with pytest.raises(RuntimeError) as error:
        ProcessLock(tmp_path / "single.lock", uuid4(), NOW).acquire()
    assert error.value.code == "runtime_already_running"
    lock.release()

    database = Database(tmp_path / "progress.sqlite")
    MigrationRunner(database).apply_all()
    with database.transaction(write=True) as connection:
        for _index in range(10):
            connection.execute(
                """
                INSERT INTO paper_runtime_sessions(
                    session_id, evidence_kind, state, started_at, ended_at,
                    clean_shutdown, reconciliation_healthy, git_commit,
                    release_manifest_hash, strategy_manifest_hash, config_hash
                ) VALUES (?, 'dry', 'STOPPED', ?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    NOW.isoformat(),
                    NOW.isoformat(),
                    "a" * 40,
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                ),
            )
    progress = runtime_progress(database)
    assert progress.successful_dry_sessions == 10
    assert progress.dry_requirement_met
    assert not progress.formal_requirement_met
