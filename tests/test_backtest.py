from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from personal_quant.backtest import (
    ArtifactWriter,
    BacktestBar,
    BacktestConfig,
    BacktestError,
    BacktestOrder,
    CompositeSlippage,
    EventDrivenBacktester,
    EventPriority,
    EventQueue,
    FillModel,
    FixedSlippage,
    HalfSpreadSlippage,
    LimitTouchFill,
    NextBarOpenFill,
    ParticipationSlippage,
    PortfolioView,
    SlippageModel,
    SymbolSlippage,
    TimeOfDaySlippage,
    VolatilitySlippage,
    deterministic_order_id,
)
from personal_quant.broker.contracts import OrderSide
from personal_quant.costs import CostConfig, CostEngine
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money

NOW = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
INSTRUMENT = InstrumentKey("NSE:INFY")
COST_CONFIG = Path("config/costs/zerodha_nse_delivery_2026-07-28.yaml")


def bar(index: int, *, open_price: str, close: str, low: str, high: str) -> BacktestBar:
    return BacktestBar(
        INSTRUMENT,
        NOW + timedelta(days=index),
        Money.from_value(open_price),
        Money.from_value(high),
        Money.from_value(low),
        Money.from_value(close),
        10_000,
    )


def bars() -> tuple[BacktestBar, ...]:
    return (
        bar(0, open_price="100", close="101", low="99", high="102"),
        bar(1, open_price="102", close="104", low="101", high="105"),
        bar(2, open_price="106", close="105", low="104", high="107"),
        bar(3, open_price="108", close="107", low="106", high="109"),
    )


def engine(
    *, fill_model: FillModel | None = None, slippage: SlippageModel | None = None
) -> EventDrivenBacktester:
    return EventDrivenBacktester(
        BacktestConfig(Money.from_value("10000"), Money.from_value("10"), Decimal("1")),
        fill_model or NextBarOpenFill(),
        slippage or FixedSlippage(Decimal("10")),
        CostEngine(CostConfig.load(COST_CONFIG)),
    )


def round_trip_signal(current: BacktestBar, portfolio: PortfolioView) -> tuple[BacktestOrder, ...]:
    if current.timestamp == NOW:
        return (
            BacktestOrder(
                deterministic_order_id("test", current, 0),
                INSTRUMENT,
                OrderSide.BUY,
                10,
                current.timestamp,
            ),
        )
    if current.timestamp == NOW + timedelta(days=2) and portfolio.positions.get(INSTRUMENT) == 10:
        return (
            BacktestOrder(
                deterministic_order_id("test", current, 0),
                INSTRUMENT,
                OrderSide.SELL,
                10,
                current.timestamp,
            ),
        )
    return ()


def test_next_bar_engine_has_no_same_bar_leakage_and_is_deterministic() -> None:
    first = engine().run(
        bars(), round_trip_signal, run_config={"strategy": "test"}, data_checksums=("abc",)
    )
    second = engine().run(
        reversed(bars()),
        round_trip_signal,
        run_config={"strategy": "test"},
        data_checksums=("abc",),
    )

    assert first == second
    assert first.fills[0].filled_at == NOW + timedelta(days=1)
    assert first.fills[1].filled_at == NOW + timedelta(days=3)
    assert first.fills[0].price == Money.from_value("102.10")
    assert first.fills[1].price == Money.from_value("107.89")
    assert first.metrics.trade_count == 1
    assert first.metrics.annualized_metrics_headlined is False
    assert first.warnings == ("annualized_metrics_suppressed_short_sample",)
    assert first.cost_scenarios["base"].variable_costs < first.cost_scenarios["1.5x"].variable_costs
    assert first.cost_scenarios["1.5x"].variable_costs < first.cost_scenarios["2.0x"].variable_costs


def test_limit_touch_waits_for_later_cross_and_uses_conservative_limit() -> None:
    source = bars()

    def signal(current: BacktestBar, _portfolio: PortfolioView) -> tuple[BacktestOrder, ...]:
        if current == source[0]:
            return (
                BacktestOrder(
                    uuid4(),
                    INSTRUMENT,
                    OrderSide.BUY,
                    1,
                    current.timestamp,
                    Money.from_value("101.50"),
                ),
            )
        return ()

    result = engine(fill_model=LimitTouchFill(), slippage=FixedSlippage(Decimal(0))).run(
        source, signal, run_config={}, data_checksums=()
    )
    assert len(result.fills) == 1
    assert result.fills[0].filled_at == source[1].timestamp
    assert result.fills[0].price == Money.from_value("101.50")


def test_event_queue_and_slippage_models_are_explicit_and_deterministic() -> None:
    queue = EventQueue()
    queue.put(NOW, EventPriority.ORDER, "second", 2)
    queue.put(NOW, EventPriority.MARKET, "first", 1)
    assert (queue.pop().kind, queue.pop().kind) == ("first", "second")
    with pytest.raises(BacktestError) as error:
        queue.pop()
    assert error.value.code == "event_queue_empty"

    current = bars()[0]
    order = BacktestOrder(uuid4(), INSTRUMENT, OrderSide.BUY, 100, NOW)
    combined = CompositeSlippage(
        (
            FixedSlippage(Decimal(1)),
            HalfSpreadSlippage(Decimal(4), Decimal(1)),
            VolatilitySlippage(Decimal(2), Decimal("1.5")),
            ParticipationSlippage(Decimal(1), Decimal("0.5")),
            SymbolSlippage({INSTRUMENT: Decimal(2)}),
            TimeOfDaySlippage(Decimal(2), {NOW.hour: Decimal("1.5")}),
        )
    )
    assert combined.bps(order, current) == Decimal("13.50")


def test_immutable_artifacts_are_complete_and_byte_reproducible(tmp_path: Path) -> None:
    result = engine().run(
        bars(), round_trip_signal, run_config={"strategy": "test"}, data_checksums=("abc",)
    )

    def write(root: Path) -> Path:
        return ArtifactWriter(root).write(
            result,
            run_config={"strategy": "test"},
            data_manifest={"checksums": ["abc"]},
            strategy_manifest={"name": "test", "version": "1"},
            git_commit="a" * 40,
            environment_lock_hash="b" * 64,
        )

    first = write(tmp_path / "one")
    second = write(tmp_path / "two")
    assert {item.name for item in first.iterdir()} == {
        "config.json",
        "cost_stress.json",
        "data_manifest.json",
        "equity.csv",
        "equity.svg",
        "fills.csv",
        "manifest.json",
        "metrics.json",
        "positions.csv",
        "run.log",
        "strategy_manifest.json",
        "trades.csv",
        "warnings.json",
    }
    for item in first.iterdir():
        assert item.read_bytes() == (second / item.name).read_bytes()
    with pytest.raises(BacktestError) as error:
        write(tmp_path / "one")
    assert error.value.code == "artifact_exists"


def test_invalid_inputs_fail_closed() -> None:
    with pytest.raises(BacktestError):
        engine().run([], round_trip_signal, run_config={}, data_checksums=())
    duplicate = bars()[0]
    with pytest.raises(BacktestError) as error:
        engine().run([duplicate, duplicate], round_trip_signal, run_config={}, data_checksums=())
    assert error.value.code == "bars_duplicate"
    with pytest.raises(BacktestError):
        FixedSlippage(Decimal(-1)).bps(
            BacktestOrder(uuid4(), INSTRUMENT, OrderSide.BUY, 1, NOW), duplicate
        )
