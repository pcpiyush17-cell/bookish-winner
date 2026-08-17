from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_benchmarks import BenchmarkPoint, BenchmarkSuite, BenchmarkSuiteConfig
from personal_quant.research_momentum import (
    MomentumChallenger,
    MomentumConfig,
    ResearchMomentumError,
)

CONFIG = Path("config/research/cross_sectional_momentum_v1.yaml")
BENCHMARKS = Path("config/research/benchmarks_v1.yaml")
CLI = CliRunner()
KEYS = ("NSE:AAA", "NSE:BBB", "NSE:CCC", "NSE:DDD")


def _config() -> MomentumConfig:
    return MomentumConfig(
        schema_version=1,
        strategy_id="momentum_test",
        lookback_observations=4,
        skip_recent_observations=1,
        minimum_universe=4,
        top_fraction=Decimal("0.50"),
        maximum_positions=2,
        rank_buffer=1,
        maximum_weight=Decimal("0.60"),
        volatility_floor=Decimal("0.001"),
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        rebalance="month_end",
        signal_execution_lag_observations=1,
        selection_window="validation",
        long_only=True,
        fractional_units=True,
        production_order_routing=False,
    )


def _points() -> tuple[BenchmarkPoint, ...]:
    start = datetime(2020, 1, 26, tzinfo=UTC)
    values = (
        (100, 100, 100, 100),
        (102, 101, 100, 99),
        (104, 102, 100, 98),
        (106, 103, 100, 97),
        (108, 104, 100, 96),
        (110, 105, 100, 95),
        (112, 106, 100, 94),
        (114, 107, 100, 93),
    )
    return tuple(
        BenchmarkPoint(
            start + timedelta(days=index),
            {key: Decimal(value) for key, value in zip(KEYS, row, strict=True)},
            KEYS,
        )
        for index, row in enumerate(values)
    )


def test_momentum_ranks_lagged_returns_and_executes_next_observation() -> None:
    result = MomentumChallenger(_config()).run(_points())

    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.signal_at == datetime(2020, 1, 31, tzinfo=UTC)
    assert decision.execute_at == datetime(2020, 2, 1, tzinfo=UTC)
    assert decision.ranked_instruments[:2] == ("NSE:AAA", "NSE:BBB")
    assert decision.selected_instruments == ("NSE:AAA", "NSE:BBB")
    assert sum(decision.target_weights.values(), Decimal(0)) == Decimal(1)
    assert max(decision.target_weights.values()) <= Decimal("0.60")
    assert result.selection_window == "validation"
    assert result.production_order_routing is False


def test_cost_stress_is_monotonic_and_results_are_immutable() -> None:
    result = MomentumChallenger(_config()).run(_points())
    returns = result.metrics.net_return_pct_by_cost

    assert returns["1.0x"] > returns["1.5x"] > returns["2.0x"]
    assert result.metrics.turnover_by_cost["1.0x"] > 0
    with pytest.raises(TypeError):
        returns["1.0x"] = Decimal(0)  # type: ignore[index]


def test_execution_price_cannot_change_the_preceding_signal_rank() -> None:
    original = MomentumChallenger(_config()).run(_points()).decisions[0]
    changed = list(_points())
    execution = changed[6]
    changed[6] = BenchmarkPoint(
        execution.timestamp,
        {**execution.prices, "NSE:DDD": Decimal("1000")},
        execution.members,
    )
    revised = MomentumChallenger(_config()).run(tuple(changed)).decisions[0]

    assert revised.ranked_instruments == original.ranked_instruments
    assert revised.selected_instruments == original.selected_instruments


def test_point_in_time_history_and_execution_prices_fail_closed() -> None:
    changed = list(_points())
    history = changed[3]
    changed[3] = BenchmarkPoint(
        history.timestamp,
        history.prices,
        ("NSE:BBB", "NSE:CCC", "NSE:DDD"),
    )
    result = MomentumChallenger(_config()).run(tuple(changed))
    assert result.decisions == ()

    changed = list(_points())
    skipped = changed[4]
    changed[4] = BenchmarkPoint(
        skipped.timestamp,
        skipped.prices,
        ("NSE:BBB", "NSE:CCC", "NSE:DDD"),
    )
    skip_two = _config().model_copy(update={"skip_recent_observations": 2})
    assert MomentumChallenger(skip_two).run(tuple(changed)).decisions == ()

    changed = list(_points())
    execution = changed[6]
    changed[6] = BenchmarkPoint(
        execution.timestamp,
        {key: value for key, value in execution.prices.items() if key != "NSE:AAA"},
        ("NSE:BBB", "NSE:CCC", "NSE:DDD"),
    )
    with pytest.raises(ResearchMomentumError) as error:
        MomentumChallenger(_config()).run(tuple(changed))
    assert error.value.code == "research_momentum_entry_price_missing"


def test_challenger_compares_only_validation_cost_cases_to_benchmarks() -> None:
    points = _points()
    momentum = MomentumChallenger(_config()).run(points)
    benchmark_config = BenchmarkSuiteConfig.load(BENCHMARKS).model_copy(
        update={"minimum_observations": 2}
    )
    comparison = momentum.compare_to(BenchmarkSuite(benchmark_config).run(points))

    assert set(comparison.excess_return_pct_by_cost) == {"1.0x", "1.5x", "2.0x"}
    assert comparison.selection_window == "validation"
    assert comparison.eligible_for_operational_promotion is False


def test_configuration_panel_and_cli_guards(tmp_path: Path) -> None:
    loaded = MomentumConfig.load(CONFIG)
    assert loaded.signal_execution_lag_observations == 1
    assert loaded.production_order_routing is False

    with pytest.raises(ResearchMomentumError) as error:
        MomentumChallenger(_config()).run(_points()[:4])
    assert error.value.code == "research_momentum_sample_short"
    with pytest.raises(ResearchMomentumError) as error:
        MomentumChallenger(_config()).run(tuple(reversed(_points())))
    assert error.value.code == "research_momentum_order_invalid"

    checked = CLI.invoke(app, ["research-momentum-check"])
    assert checked.exit_code == 0
    assert "Signal execution lag: 1 observation" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-momentum-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_momentum_config_invalid" in failed.stderr
