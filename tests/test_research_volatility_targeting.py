from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_volatility_targeting import (
    ResearchVolatilityTargetingError,
    VolatilityReturnPoint,
    VolatilityTargetConfig,
    VolatilityTargetOverlay,
)

CONFIG = Path("config/research/volatility_targeting_v1.yaml")
CLI = CliRunner()


def _config() -> VolatilityTargetConfig:
    return VolatilityTargetConfig(
        schema_version=1,
        overlay_id="volatility_test",
        lookback_observations=5,
        annualization_observations=252,
        target_annual_volatility=Decimal("0.10"),
        volatility_floor=Decimal("0.01"),
        minimum_exposure=Decimal("0"),
        maximum_exposure=Decimal("1"),
        maximum_exposure_step=Decimal("0.25"),
        rebalance_threshold=Decimal("0.05"),
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        signal_execution_lag_observations=1,
        selection_window="validation",
        cash_return=Decimal("0"),
        production_order_routing=False,
    )


def _points(returns: tuple[str, ...] | None = None) -> tuple[VolatilityReturnPoint, ...]:
    values = returns or (
        "0.001",
        "-0.001",
        "0.001",
        "-0.001",
        "0.001",
        "0.002",
        "0.001",
        "0.002",
        "0.001",
        "0.002",
    )
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return tuple(
        VolatilityReturnPoint(start + timedelta(days=index), Decimal(value))
        for index, value in enumerate(values)
    )


def test_trailing_volatility_scales_exposure_with_next_observation_execution() -> None:
    result = VolatilityTargetOverlay(_config()).run(_points())

    assert result.decisions[0].signal_at == datetime(2020, 1, 5, tzinfo=UTC)
    assert result.decisions[0].execute_at == datetime(2020, 1, 6, tzinfo=UTC)
    assert result.decisions[0].target_exposure == Decimal("0.25")
    assert result.decisions[1].target_exposure == Decimal("0.50")
    assert all(
        Decimal(0) <= decision.target_exposure <= Decimal(1) for decision in result.decisions
    )
    assert result.selection_window == "validation"
    assert result.production_order_routing is False


def test_high_volatility_deleverages_and_zero_volatility_uses_floor() -> None:
    high_volatility = _points(("0.10", "-0.10", "0.10", "-0.10", "0.10", "-0.10", "0.10"))
    high_result = VolatilityTargetOverlay(_config()).run(high_volatility)
    assert high_result.decisions[0].estimated_annual_volatility > Decimal("1")
    assert high_result.decisions[0].target_exposure < Decimal("0.10")

    flat = _points(("0", "0", "0", "0", "0", "0", "0"))
    flat_result = VolatilityTargetOverlay(_config()).run(flat)
    assert flat_result.decisions[0].estimated_annual_volatility == Decimal("0.01")
    assert flat_result.decisions[0].unconstrained_exposure == Decimal("10")
    assert flat_result.decisions[0].target_exposure == Decimal("0.25")


def test_future_return_cannot_change_preceding_exposure_decision() -> None:
    original = VolatilityTargetOverlay(_config()).run(_points()).decisions[0]
    changed = list(_points())
    changed[5] = VolatilityReturnPoint(changed[5].timestamp, Decimal("0.50"))
    revised = VolatilityTargetOverlay(_config()).run(tuple(changed)).decisions[0]

    assert revised == original


def test_cost_stress_is_monotonic_and_metrics_are_immutable() -> None:
    result = VolatilityTargetOverlay(_config()).run(_points())
    returns = result.metrics.net_return_pct_by_cost

    assert returns["1.0x"] > returns["1.5x"] > returns["2.0x"]
    assert result.metrics.turnover_by_cost["1.0x"] > 0
    assert set(result.metrics.excess_return_pct_vs_static_by_cost) == {
        "1.0x",
        "1.5x",
        "2.0x",
    }
    with pytest.raises(TypeError):
        returns["1.0x"] = Decimal(0)  # type: ignore[index]


def test_point_panel_and_configuration_fail_closed() -> None:
    with pytest.raises(ResearchVolatilityTargetingError) as error:
        VolatilityReturnPoint(datetime(2020, 1, 1), Decimal("0.01"))
    assert error.value.code == "research_volatility_time_naive"
    with pytest.raises(ResearchVolatilityTargetingError) as error:
        VolatilityReturnPoint(datetime.now(UTC), Decimal("-1"))
    assert error.value.code == "research_volatility_return_invalid"
    with pytest.raises(ResearchVolatilityTargetingError) as error:
        VolatilityTargetOverlay(_config()).run(_points()[:5])
    assert error.value.code == "research_volatility_sample_short"
    with pytest.raises(ResearchVolatilityTargetingError) as error:
        VolatilityTargetOverlay(_config()).run(tuple(reversed(_points())))
    assert error.value.code == "research_volatility_order_invalid"

    invalid_config = _config().model_dump()
    invalid_config.update({"minimum_exposure": Decimal("1"), "maximum_exposure": Decimal("0.5")})
    with pytest.raises(ValidationError, match="minimum exposure"):
        VolatilityTargetConfig.model_validate(invalid_config)


def test_versioned_config_and_cli_are_read_only(tmp_path: Path) -> None:
    loaded = VolatilityTargetConfig.load(CONFIG)
    assert loaded.maximum_exposure == Decimal("1.00")
    assert loaded.production_order_routing is False

    checked = CLI.invoke(app, ["research-volatility-targeting-check"])
    assert checked.exit_code == 0
    assert "Maximum exposure: 1.00x (unlevered)" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-volatility-targeting-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_volatility_config_invalid" in failed.stderr
