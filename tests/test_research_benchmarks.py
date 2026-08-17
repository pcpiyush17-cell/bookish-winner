from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_benchmarks import (
    BenchmarkPoint,
    BenchmarkSuite,
    BenchmarkSuiteConfig,
    ResearchBenchmarkError,
    compare_challenger,
)

CONFIG = Path("config/research/benchmarks_v1.yaml")
CLI = CliRunner()


def _config() -> BenchmarkSuiteConfig:
    return BenchmarkSuiteConfig.load(CONFIG).model_copy(update={"minimum_observations": 3})


def _points() -> tuple[BenchmarkPoint, ...]:
    start = datetime(2020, 1, 30, tzinfo=UTC)
    return (
        BenchmarkPoint(
            start,
            {"NSE:AAA": Decimal("100"), "NSE:BBB": Decimal("100")},
            ("NSE:AAA", "NSE:BBB"),
        ),
        BenchmarkPoint(
            start + timedelta(days=1),
            {"NSE:AAA": Decimal("110"), "NSE:BBB": Decimal("95")},
            ("NSE:AAA", "NSE:BBB"),
        ),
        BenchmarkPoint(
            start + timedelta(days=2),
            {"NSE:AAA": Decimal("120"), "NSE:BBB": Decimal("100")},
            ("NSE:AAA", "NSE:BBB"),
        ),
    )


def test_suite_runs_all_controls_with_cost_stress_and_no_routing() -> None:
    result = BenchmarkSuite(_config()).run(_points())

    assert result.selection_window == "validation"
    assert result.production_order_routing is False
    assert len(result.results) == 4
    cash = result.result("cash")
    buy_hold = result.result("equal_weight_buy_hold")
    daily = result.result("equal_weight_daily")
    assert set(cash.net_return_pct_by_cost) == {"1.0x", "1.5x", "2.0x"}
    assert all(value == 0 for value in cash.net_return_pct_by_cost.values())
    assert buy_hold.net_return_pct_by_cost["1.0x"] > buy_hold.net_return_pct_by_cost["2.0x"]
    assert daily.turnover_by_cost["1.0x"] >= buy_hold.turnover_by_cost["1.0x"]
    with pytest.raises(TypeError):
        buy_hold.net_return_pct_by_cost["1.0x"] = Decimal(0)  # type: ignore[index]


def test_monthly_rebalances_only_at_month_boundary() -> None:
    result = BenchmarkSuite(_config()).run(_points())
    monthly = result.result("equal_weight_monthly")
    buy_hold = result.result("equal_weight_buy_hold")

    assert monthly.turnover_by_cost["1.0x"] > buy_hold.turnover_by_cost["1.0x"]


def test_membership_change_uses_point_in_time_members_and_requires_exit_price() -> None:
    points = list(_points())
    points[-1] = BenchmarkPoint(
        points[-1].timestamp,
        {
            "NSE:AAA": Decimal("120"),
            "NSE:BBB": Decimal("100"),
            "NSE:CCC": Decimal("50"),
        },
        ("NSE:AAA", "NSE:CCC"),
    )
    result = BenchmarkSuite(_config()).run(tuple(points))
    assert result.result("equal_weight_buy_hold").turnover_by_cost["1.0x"] > Decimal(2)

    points[-1] = BenchmarkPoint(
        points[-1].timestamp,
        {"NSE:AAA": Decimal("120"), "NSE:CCC": Decimal("50")},
        ("NSE:AAA", "NSE:CCC"),
    )
    with pytest.raises(ResearchBenchmarkError) as error:
        BenchmarkSuite(_config()).run(tuple(points))
    assert error.value.code == "research_benchmark_exit_price_missing"


def test_challenger_compares_validation_against_strongest_control() -> None:
    suite = BenchmarkSuite(_config()).run(_points())
    comparison = compare_challenger(
        validation_net_return_pct_by_cost={
            "1.0x": Decimal("20"),
            "1.5x": Decimal("19"),
            "2.0x": Decimal("18"),
        },
        suite=suite,
    )

    assert comparison.beats_all_cost_cases is True
    assert comparison.selection_window == "validation"
    assert comparison.eligible_for_operational_promotion is False
    with pytest.raises(ResearchBenchmarkError, match="every cost"):
        compare_challenger(validation_net_return_pct_by_cost={"1.0x": Decimal(1)}, suite=suite)


def test_panel_and_point_validation_fail_closed() -> None:
    with pytest.raises(ResearchBenchmarkError) as error:
        BenchmarkPoint(datetime(2020, 1, 1), {"NSE:AAA": Decimal(1)}, ("NSE:AAA",))
    assert error.value.code == "research_benchmark_time_naive"
    with pytest.raises(ResearchBenchmarkError) as error:
        BenchmarkPoint(datetime.now(UTC), {"NSE:AAA": Decimal(1)}, ())
    assert error.value.code == "research_benchmark_members_invalid"
    with pytest.raises(ResearchBenchmarkError) as error:
        BenchmarkPoint(datetime.now(UTC), {}, ("NSE:AAA",))
    assert error.value.code == "research_benchmark_prices_invalid"
    with pytest.raises(ResearchBenchmarkError) as error:
        BenchmarkSuite(_config()).run(_points()[:2])
    assert error.value.code == "research_benchmark_sample_short"
    with pytest.raises(ResearchBenchmarkError) as error:
        BenchmarkSuite(_config()).run(tuple(reversed(_points())))
    assert error.value.code == "research_benchmark_order_invalid"


def test_config_and_cli_are_fail_closed_and_read_only(tmp_path: Path) -> None:
    checked = CLI.invoke(app, ["research-benchmarks-check"])
    assert checked.exit_code == 0
    assert "Selection window: validation only" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-benchmarks-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_benchmark_config_invalid" in failed.stderr
