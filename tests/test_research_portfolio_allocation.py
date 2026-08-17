from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_portfolio_allocation import (
    AllocationPoint,
    CorrelationAwareAllocator,
    PortfolioAllocationConfig,
    ResearchPortfolioAllocationError,
)

CONFIG = Path("config/research/portfolio_allocation_v1.yaml")
CLI = CliRunner()
KEYS = ("momentum", "mean_reversion", "defensive")


def _config() -> PortfolioAllocationConfig:
    return PortfolioAllocationConfig(
        schema_version=1,
        allocator_id="allocation_test",
        lookback_observations=5,
        minimum_strategies=2,
        volatility_floor=Decimal("0.0001"),
        correlation_penalty=Decimal("1"),
        maximum_strategy_weight=Decimal("0.60"),
        rebalance_threshold=Decimal("0.01"),
        maximum_one_way_turnover=Decimal("0.50"),
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        signal_execution_lag_observations=1,
        selection_window="validation",
        long_only=True,
        cash_allowed=True,
        production_order_routing=False,
    )


def _points() -> tuple[AllocationPoint, ...]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    rows = (
        ("0.01", "0.01", "-0.01"),
        ("-0.01", "-0.01", "0.01"),
        ("0.01", "0.01", "-0.01"),
        ("-0.01", "-0.01", "0.01"),
        ("0.01", "0.01", "-0.01"),
        ("0.02", "0.02", "0.01"),
        ("0.01", "0.02", "0.01"),
        ("0.02", "0.01", "0.02"),
    )
    return tuple(
        AllocationPoint(
            start + timedelta(days=index),
            {key: Decimal(value) for key, value in zip(KEYS, row, strict=True)},
            KEYS,
        )
        for index, row in enumerate(rows)
    )


def test_correlation_penalty_diversifies_and_executes_next_observation() -> None:
    result = CorrelationAwareAllocator(_config()).run(_points())

    decision = result.decisions[0]
    assert decision.signal_at == datetime(2020, 1, 5, tzinfo=UTC)
    assert decision.execute_at == datetime(2020, 1, 6, tzinfo=UTC)
    assert decision.estimated_correlation["momentum"]["mean_reversion"] > Decimal("0.999")
    assert decision.estimated_correlation["momentum"]["defensive"] < Decimal("-0.999")
    assert decision.unconstrained_weights["defensive"] > decision.unconstrained_weights["momentum"]
    assert abs(sum(decision.target_weights.values(), Decimal(0)) - Decimal("0.50")) < Decimal(
        "1e-25"
    )
    assert result.selection_window == "validation"
    assert result.production_order_routing is False


def test_weight_cap_turnover_ramp_and_rebalance_threshold_are_enforced() -> None:
    points = list(_points())
    low_high_rows = (
        ("0.001", "0.03", "-0.03"),
        ("-0.001", "-0.03", "0.03"),
        ("0.001", "0.03", "-0.03"),
        ("-0.001", "-0.03", "0.03"),
        ("0.001", "0.03", "-0.03"),
    )
    for index, row in enumerate(low_high_rows):
        points[index] = AllocationPoint(
            points[index].timestamp,
            {key: Decimal(value) for key, value in zip(KEYS, row, strict=True)},
            KEYS,
        )
    result = CorrelationAwareAllocator(_config()).run(tuple(points))
    first = result.decisions[0]

    assert first.unconstrained_weights["momentum"] > Decimal("0.60")
    assert first.target_weights["momentum"] == Decimal("0.30")
    assert max(first.target_weights.values()) <= Decimal("0.30")
    assert sum(result.decisions[1].target_weights.values(), Decimal(0)) == Decimal(1)

    sticky = _config().model_copy(update={"rebalance_threshold": Decimal("1")})
    sticky_result = CorrelationAwareAllocator(sticky).run(_points())
    assert sticky_result.decisions[1].target_weights == sticky_result.decisions[0].target_weights


def test_future_returns_cannot_change_preceding_allocation() -> None:
    original = CorrelationAwareAllocator(_config()).run(_points()).decisions[0]
    changed = list(_points())
    execution = changed[5]
    changed[5] = AllocationPoint(
        execution.timestamp,
        {**execution.strategy_returns, "defensive": Decimal("0.90")},
        execution.available_strategies,
    )
    revised = CorrelationAwareAllocator(_config()).run(tuple(changed)).decisions[0]

    assert revised == original


def test_point_in_time_eligibility_and_execution_fail_closed() -> None:
    changed = list(_points())
    history = changed[2]
    changed[2] = AllocationPoint(
        history.timestamp,
        history.strategy_returns,
        ("mean_reversion", "defensive"),
    )
    decision = CorrelationAwareAllocator(_config()).run(tuple(changed)).decisions[0]
    assert decision.eligible_strategies == ("mean_reversion", "defensive")
    assert "momentum" not in decision.target_weights

    changed = list(_points())
    execution = changed[5]
    changed[5] = AllocationPoint(
        execution.timestamp,
        execution.strategy_returns,
        ("momentum", "mean_reversion"),
    )
    with pytest.raises(ResearchPortfolioAllocationError) as error:
        CorrelationAwareAllocator(_config()).run(tuple(changed))
    assert error.value.code == "research_allocation_execution_unavailable"


def test_cost_stress_control_and_nested_results_are_immutable() -> None:
    result = CorrelationAwareAllocator(_config()).run(_points())
    returns = result.metrics.net_return_pct_by_cost

    assert returns["1.0x"] > returns["1.5x"] > returns["2.0x"]
    assert result.metrics.turnover_by_cost["1.0x"] > 0
    assert set(result.metrics.excess_return_pct_vs_equal_weight_by_cost) == {
        "1.0x",
        "1.5x",
        "2.0x",
    }
    with pytest.raises(TypeError):
        returns["1.0x"] = Decimal(0)  # type: ignore[index]
    with pytest.raises(TypeError):
        result.decisions[0].estimated_correlation["momentum"]["defensive"] = Decimal(0)  # type: ignore[index]


def test_configuration_panel_point_and_cli_guards(tmp_path: Path) -> None:
    loaded = PortfolioAllocationConfig.load(CONFIG)
    assert loaded.signal_execution_lag_observations == 1
    assert loaded.production_order_routing is False

    with pytest.raises(ResearchPortfolioAllocationError) as error:
        AllocationPoint(datetime(2020, 1, 1), {"a": Decimal(0)}, ("a",))
    assert error.value.code == "research_allocation_time_naive"
    with pytest.raises(ResearchPortfolioAllocationError) as error:
        AllocationPoint(datetime.now(UTC), {"a": Decimal("-1")}, ("a",))
    assert error.value.code == "research_allocation_returns_invalid"
    with pytest.raises(ResearchPortfolioAllocationError) as error:
        CorrelationAwareAllocator(_config()).run(_points()[:5])
    assert error.value.code == "research_allocation_sample_short"
    with pytest.raises(ResearchPortfolioAllocationError) as error:
        CorrelationAwareAllocator(_config()).run(tuple(reversed(_points())))
    assert error.value.code == "research_allocation_order_invalid"

    checked = CLI.invoke(app, ["research-portfolio-allocation-check"])
    assert checked.exit_code == 0
    assert "Maximum strategy weight: 0.60" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-portfolio-allocation-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_allocation_config_invalid" in failed.stderr
