from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_benchmarks import BenchmarkPoint, BenchmarkSuite, BenchmarkSuiteConfig
from personal_quant.research_mean_reversion import (
    MeanReversionChallenger,
    MeanReversionConfig,
    MeanReversionPoint,
    ResearchMeanReversionError,
)

CONFIG = Path("config/research/mean_reversion_regime_v1.yaml")
BENCHMARKS = Path("config/research/benchmarks_v1.yaml")
CLI = CliRunner()
KEYS = ("NSE:AAA", "NSE:BBB", "NSE:CCC", "NSE:DDD")


def _config() -> MeanReversionConfig:
    return MeanReversionConfig(
        schema_version=1,
        strategy_id="mean_reversion_test",
        signal_lookback_observations=2,
        regime_lookback_observations=5,
        minimum_universe=4,
        entry_zscore=Decimal("-1.0"),
        exit_zscore=Decimal("-0.25"),
        bottom_fraction=Decimal("0.25"),
        maximum_positions=2,
        maximum_weight=Decimal("0.60"),
        minimum_dollar_volume=Decimal("100"),
        trend_threshold=Decimal("0.50"),
        volatility_threshold=Decimal("0.50"),
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        signal_execution_lag_observations=1,
        selection_window="validation",
        long_only=True,
        fractional_units=True,
        production_order_routing=False,
    )


def _points() -> tuple[MeanReversionPoint, ...]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    values = (
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (100, 100, 100, 100),
        (98, 101, 100, 100),
        (90, 102, 100, 100),
        (95, 101, 100, 100),
    )
    return tuple(
        MeanReversionPoint(
            start + timedelta(days=index),
            {key: Decimal(value) for key, value in zip(KEYS, row, strict=True)},
            {key: Decimal("1000") for key in KEYS},
            KEYS,
        )
        for index, row in enumerate(values)
    )


def _benchmark_points(points: tuple[MeanReversionPoint, ...]) -> tuple[BenchmarkPoint, ...]:
    return tuple(BenchmarkPoint(point.timestamp, point.prices, point.members) for point in points)


def test_range_regime_selects_recent_loser_and_executes_next_observation() -> None:
    result = MeanReversionChallenger(_config()).run(_points())

    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.regime == "RANGE_NORMAL"
    assert decision.signal_at == datetime(2020, 1, 6, tzinfo=UTC)
    assert decision.execute_at == datetime(2020, 1, 7, tzinfo=UTC)
    assert decision.selected_instruments == ("NSE:AAA",)
    assert decision.target_weights == {"NSE:AAA": Decimal("0.60")}
    assert result.metrics.active_decisions == 1
    assert result.selection_window == "validation"
    assert result.production_order_routing is False


def test_trending_and_high_volatility_regimes_target_cash() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    trending = tuple(
        MeanReversionPoint(
            start + timedelta(days=index),
            {key: Decimal(100 + 10 * index) for key in KEYS},
            {key: Decimal("1000") for key in KEYS},
            KEYS,
        )
        for index in range(7)
    )
    trend_config = _config().model_copy(update={"trend_threshold": Decimal("0.20")})
    trend_result = MeanReversionChallenger(trend_config).run(trending)
    assert trend_result.decisions[0].regime == "TRENDING"
    assert trend_result.decisions[0].target_weights == {}

    volatile_rows = (100, 130, 90, 140, 80, 150, 100)
    volatile = tuple(
        MeanReversionPoint(
            start + timedelta(days=index),
            {key: Decimal(value) for key in KEYS},
            {key: Decimal("1000") for key in KEYS},
            KEYS,
        )
        for index, value in enumerate(volatile_rows)
    )
    volatile_config = _config().model_copy(update={"volatility_threshold": Decimal("0.10")})
    volatility_result = MeanReversionChallenger(volatile_config).run(volatile)
    assert volatility_result.decisions[0].regime == "HIGH_VOLATILITY"
    assert volatility_result.decisions[0].target_weights == {}


def test_execution_price_cannot_change_preceding_signal() -> None:
    original = MeanReversionChallenger(_config()).run(_points()).decisions[0]
    changed = list(_points())
    execution = changed[-1]
    changed[-1] = MeanReversionPoint(
        execution.timestamp,
        {**execution.prices, "NSE:DDD": Decimal("1000")},
        execution.dollar_volumes,
        execution.members,
    )
    revised = MeanReversionChallenger(_config()).run(tuple(changed)).decisions[0]

    assert revised.selected_instruments == original.selected_instruments
    assert revised.target_weights == original.target_weights


def test_history_liquidity_and_execution_availability_fail_closed() -> None:
    changed = list(_points())
    history = changed[3]
    changed[3] = MeanReversionPoint(
        history.timestamp,
        history.prices,
        history.dollar_volumes,
        ("NSE:BBB", "NSE:CCC", "NSE:DDD"),
    )
    assert MeanReversionChallenger(_config()).run(tuple(changed)).decisions == ()

    changed = list(_points())
    signal = changed[5]
    changed[5] = MeanReversionPoint(
        signal.timestamp,
        signal.prices,
        {**signal.dollar_volumes, "NSE:AAA": Decimal("1")},
        signal.members,
    )
    assert MeanReversionChallenger(_config()).run(tuple(changed)).decisions == ()

    changed = list(_points())
    execution = changed[-1]
    changed[-1] = MeanReversionPoint(
        execution.timestamp,
        execution.prices,
        execution.dollar_volumes,
        ("NSE:BBB", "NSE:CCC", "NSE:DDD"),
    )
    with pytest.raises(ResearchMeanReversionError) as error:
        MeanReversionChallenger(_config()).run(tuple(changed))
    assert error.value.code == "research_mean_reversion_entry_price_missing"


def test_cost_stress_comparison_and_results_are_read_only() -> None:
    points = _points()
    result = MeanReversionChallenger(_config()).run(points)
    returns = result.metrics.net_return_pct_by_cost

    assert returns["1.0x"] > returns["1.5x"] > returns["2.0x"]
    assert result.metrics.turnover_by_cost["1.0x"] > 0
    with pytest.raises(TypeError):
        returns["1.0x"] = Decimal(0)  # type: ignore[index]

    benchmark_config = BenchmarkSuiteConfig.load(BENCHMARKS).model_copy(
        update={"minimum_observations": 2}
    )
    comparison = result.compare_to(BenchmarkSuite(benchmark_config).run(_benchmark_points(points)))
    assert set(comparison.excess_return_pct_by_cost) == {"1.0x", "1.5x", "2.0x"}
    assert comparison.selection_window == "validation"
    assert comparison.eligible_for_operational_promotion is False


def test_configuration_panel_point_and_cli_guards(tmp_path: Path) -> None:
    loaded = MeanReversionConfig.load(CONFIG)
    assert loaded.signal_execution_lag_observations == 1
    assert loaded.production_order_routing is False

    with pytest.raises(ResearchMeanReversionError) as error:
        MeanReversionChallenger(_config()).run(_points()[:5])
    assert error.value.code == "research_mean_reversion_sample_short"
    with pytest.raises(ResearchMeanReversionError) as error:
        MeanReversionChallenger(_config()).run(tuple(reversed(_points())))
    assert error.value.code == "research_mean_reversion_order_invalid"
    with pytest.raises(ResearchMeanReversionError) as error:
        MeanReversionPoint(datetime(2020, 1, 1), {"NSE:AAA": Decimal(1)}, {}, ("NSE:AAA",))
    assert error.value.code == "research_mean_reversion_time_naive"

    checked = CLI.invoke(app, ["research-mean-reversion-check"])
    assert checked.exit_code == 0
    assert "Regime filter: trend and high-volatility risk blocked" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-mean-reversion-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_mean_reversion_config_invalid" in failed.stderr
