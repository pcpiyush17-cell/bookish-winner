from decimal import Decimal
from pathlib import Path

import pytest

from personal_quant.costs import (
    CostConfig,
    CostEngine,
    CostError,
    DeliveryTrade,
    FixedBpsModel,
    HalfSpreadPlusBpsModel,
    VolatilityScaledModel,
)

CONFIG = Path("config/costs/zerodha_nse_delivery_2026-07-28.yaml")


def engine() -> CostEngine:
    return CostEngine(CostConfig.load(CONFIG))


def test_manual_delivery_example_matches_to_paise() -> None:
    trade = DeliveryTrade(100, Decimal("100"), Decimal("110")).with_execution_models(
        spread=FixedBpsModel(Decimal("10")),
        slippage=FixedBpsModel(Decimal("5")),
    )
    result = engine().estimate(trade)
    assert result.buy_turnover == Decimal("10000.00")
    assert result.sell_turnover == Decimal("11000.00")
    assert result.brokerage == Decimal("0.00")
    assert result.stt == Decimal("21.00")
    assert result.exchange_transaction_charge == Decimal("0.64")
    assert result.sebi_turnover_charge == Decimal("0.02")
    assert result.gst == Decimal("0.12")
    assert result.stamp_duty == Decimal("1.50")
    assert result.dp_charge == Decimal("15.34")
    assert result.spread == Decimal("21.00")
    assert result.slippage == Decimal("10.50")
    assert result.variable_total == Decimal("70.12")
    assert result.gross_pnl == Decimal("1000.00")
    assert result.trading_net_pnl == Decimal("929.88")
    assert result.calculation_version == "zerodha_nse_delivery_2026-07-28_v1"
    assert result.cost_kind == "estimate"


def test_stress_scenarios_scale_execution_cost_only() -> None:
    trade = DeliveryTrade(
        100,
        Decimal("100"),
        Decimal("110"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
    )
    scenarios = engine().stress_scenarios(trade)
    assert scenarios["base"].variable_total == Decimal("70.12")
    assert scenarios["1.5x"].variable_total == Decimal("85.87")
    assert scenarios["2.0x"].variable_total == Decimal("101.62")
    assert scenarios["2.0x"].stt == scenarios["base"].stt


def test_execution_models_are_explicit() -> None:
    assert HalfSpreadPlusBpsModel(Decimal("8"), Decimal("2")).bps() == Decimal("6")
    assert VolatilityScaledModel(Decimal("12"), Decimal("0.5")).bps() == Decimal("6.0")
    with pytest.raises(CostError, match="negative"):
        FixedBpsModel(Decimal("-1")).bps()


def test_fixed_allocation_and_break_even_manual_example() -> None:
    cost_engine = engine()
    assert cost_engine.allocated_fixed_cost(20) == Decimal("25.00")
    report = cost_engine.break_even(
        starting_capital=Decimal("100000"),
        expected_variable_costs=Decimal("100"),
        target_net_profit=Decimal("1000"),
    )
    assert report.break_even_rupees == Decimal("1600.00")
    assert report.required_monthly_gross_return_pct == Decimal("1.6000")
    assert report.api_cost_pct_of_capital == Decimal("0.5000")
    assert report.capital_for_fixed_cost_below_1_pct == Decimal("50000.00")
    assert report.capital_for_fixed_cost_below_half_pct == Decimal("100000.00")
    assert report.capital_for_fixed_cost_below_quarter_pct == Decimal("200000.00")


def test_invalid_inputs_and_unverified_config_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CostError, match="positive"):
        DeliveryTrade(0, Decimal("1"), Decimal("1"))
    with pytest.raises(CostError, match="positive"):
        engine().allocated_fixed_cost(0)
    with pytest.raises(CostError, match="Capital"):
        engine().break_even(starting_capital=Decimal(0), expected_variable_costs=Decimal(0))
    invalid = tmp_path / "costs.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(CostError, match="cannot be loaded"):
        CostConfig.load(invalid)
