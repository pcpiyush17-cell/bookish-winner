import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_quant.backtest import (
    BacktestBar,
    BacktestConfig,
    EventDrivenBacktester,
    FixedSlippage,
    NextBarOpenFill,
)
from personal_quant.costs import CostConfig, CostEngine
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.paper import MarketBar
from personal_quant.strategy import (
    BaselineMomentumConfig,
    BaselineMomentumStrategy,
    MarketEvent,
    PortfolioSnapshot,
    Signal,
    SignalDirection,
    SignalPurpose,
    StrategyContext,
    StrategyError,
    StrategyRunner,
    buy_and_hold_benchmark,
    strategy_manifest,
)
from personal_quant.strategy_adapters import (
    BacktestStrategyAdapter,
    PaperStrategyAdapter,
    desired_side,
)

NOW = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
INSTRUMENT = InstrumentKey("NSE:INFY")
CONFIG = Path("config/strategies/baseline_momentum_v1.yaml")
COST_CONFIG = Path("config/costs/zerodha_nse_delivery_2026-07-28.yaml")


def events() -> tuple[MarketEvent, ...]:
    prices = ("100", "100", "101", "103", "106", "108", "109")
    return tuple(
        MarketEvent(
            INSTRUMENT,
            NOW + timedelta(days=index),
            Money.from_value(price),
            Money.from_value(Decimal(price) + 1),
            Money.from_value(Decimal(price) - 1),
            Money.from_value(price),
            20_000,
        )
        for index, price in enumerate(prices)
    )


def portfolio(quantity: int = 0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        Money.from_value("10000"),
        {INSTRUMENT: quantity} if quantity else {},
        {INSTRUMENT: Money.from_value("106")} if quantity else {},
    )


def test_baseline_emits_complete_deterministic_entry_signal() -> None:
    config = BaselineMomentumConfig.load(CONFIG)
    runner = StrategyRunner(BaselineMomentumStrategy(config), "backtest")
    runner.start(NOW)
    emitted: list[Signal] = []
    for event in events()[:5]:
        emitted.extend(runner.on_market_event(event, portfolio()))

    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.direction is SignalDirection.LONG
    assert signal.purpose is SignalPurpose.ENTRY
    assert signal.target_position == config.target_quantity
    assert signal.reason_codes == (
        "trend_positive",
        "liquidity_pass",
        "volatility_pass",
        "regime_pass",
    )
    assert set(signal.feature_snapshot) == {
        "close",
        "fast_average",
        "slow_average",
        "volatility_pct",
        "average_traded_value",
        "regime_positive",
    }
    assert signal.expires_at > signal.timestamp


def test_identical_strategy_runs_in_backtest_and_paper_modes() -> None:
    config = BaselineMomentumConfig.load(CONFIG)
    source = events()
    backtest_adapter = BacktestStrategyAdapter(
        StrategyRunner(BaselineMomentumStrategy(config), "backtest")
    )
    backtest_bars = tuple(
        BacktestBar(
            item.instrument,
            item.timestamp,
            item.open,
            item.high,
            item.low,
            item.close,
            item.volume,
        )
        for item in source
    )
    result = EventDrivenBacktester(
        BacktestConfig(Money.from_value("10000")),
        NextBarOpenFill(),
        FixedSlippage(Decimal(0)),
        CostEngine(CostConfig.load(COST_CONFIG)),
    ).run(
        backtest_bars,
        backtest_adapter,
        run_config={"strategy": strategy_manifest(config)},
        data_checksums=("fixture",),
    )
    assert len(result.fills) == 1
    assert result.fills[0].filled_at > source[4].timestamp

    paper_adapter = PaperStrategyAdapter(StrategyRunner(BaselineMomentumStrategy(config), "paper"))
    paper_signals: list[Signal] = []
    for item in source[:5]:
        paper_signals.extend(
            paper_adapter.on_bar(
                MarketBar(
                    item.instrument,
                    item.timestamp,
                    item.open,
                    item.high,
                    item.low,
                    item.close,
                    item.volume,
                ),
                cash=Money.from_value("10000"),
                positions={},
            )
        )
    assert len(paper_signals) == 1
    assert paper_signals[0].signal_id == result.fills[0].order_id
    entry_side = desired_side(paper_signals[0], 0)
    assert entry_side is not None and entry_side.value == "BUY"


def test_exit_rules_benchmark_and_manifest_are_auditable() -> None:
    config = BaselineMomentumConfig.load(CONFIG)
    runner = StrategyRunner(BaselineMomentumStrategy(config), "paper")
    runner.start(NOW)
    source = events()
    for event in source[:5]:
        runner.on_market_event(event, portfolio())
    falling = MarketEvent(
        INSTRUMENT,
        source[5].timestamp,
        Money.from_value("90"),
        Money.from_value("91"),
        Money.from_value("89"),
        Money.from_value("90"),
        20_000,
    )
    signals = runner.on_market_event(falling, portfolio(10))
    assert signals[0].direction is SignalDirection.FLAT
    assert signals[0].purpose is SignalPurpose.EXIT
    assert {"trend_reversal", "risk_stop"}.issubset(signals[0].reason_codes)
    exit_side = desired_side(signals[0], 10)
    assert exit_side is not None and exit_side.value == "SELL"

    benchmark = buy_and_hold_benchmark(source, Money.from_value("10000"))
    assert benchmark.quantity == 100
    assert benchmark.ending_capital == Money.from_value("10900")
    assert benchmark.return_pct == Decimal("9.0000")
    manifest = strategy_manifest(config)
    assert manifest["config_hash"] == config.fingerprint()
    assert manifest["strategy_id"] == "baseline_momentum_v1"


def test_config_and_lifecycle_fail_closed(tmp_path: Path) -> None:
    config = BaselineMomentumConfig.load(CONFIG)
    strategy = BaselineMomentumStrategy(config)
    state_runner = StrategyRunner(strategy, "paper")
    with pytest.raises(StrategyError) as error:
        state_runner.on_market_event(events()[0], portfolio())
    assert error.value.code == "runner_not_started"
    with pytest.raises(StrategyError):
        strategy.on_market_event(
            events()[0],
            # Never reached because strategy is not started.
            object(),  # type: ignore[arg-type]
            portfolio(),
        )
    with pytest.raises(StrategyError):
        StrategyContext("live", NOW)

    raw = CONFIG.read_text(encoding="utf-8").replace("fast_window: 3", "fast_window: 5")
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(raw, encoding="utf-8")
    with pytest.raises(StrategyError) as error:
        BaselineMomentumConfig.load(invalid)
    assert error.value.code == "strategy_config_invalid"
    with pytest.raises(ValidationError):
        BaselineMomentumConfig.model_validate({**config.model_dump(), "unexpected": True})


def test_strategy_module_has_no_broker_dependency() -> None:
    source = Path("src/personal_quant/strategy.py").read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all("broker" not in ast.unparse(node) for node in imports)
