"""Leakage-safe volatility-targeting research overlay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchVolatilityTargetingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class VolatilityTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    overlay_id: str = Field(min_length=1)
    lookback_observations: int = Field(ge=5)
    annualization_observations: int = Field(gt=1)
    target_annual_volatility: Decimal = Field(gt=0)
    volatility_floor: Decimal = Field(gt=0)
    minimum_exposure: Decimal = Field(ge=0, le=1)
    maximum_exposure: Decimal = Field(gt=0, le=1)
    maximum_exposure_step: Decimal = Field(gt=0, le=1)
    rebalance_threshold: Decimal = Field(ge=0, le=1)
    one_way_cost_bps: Decimal = Field(ge=0)
    cost_multipliers: tuple[Decimal, ...]
    signal_execution_lag_observations: Literal[1]
    selection_window: Literal["validation"]
    cash_return: Decimal
    production_order_routing: Literal[False]

    @field_validator(
        "target_annual_volatility",
        "volatility_floor",
        "minimum_exposure",
        "maximum_exposure",
        "maximum_exposure_step",
        "rebalance_threshold",
        "one_way_cost_bps",
        "cost_multipliers",
        "cash_return",
        mode="before",
    )
    @classmethod
    def parse_decimals(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(Decimal(str(item)) for item in value)
        return Decimal(str(value)) if isinstance(value, (str, int, float)) else value

    @model_validator(mode="after")
    def complete_contract(self) -> VolatilityTargetConfig:
        if self.minimum_exposure > self.maximum_exposure:
            raise ValueError("minimum exposure cannot exceed maximum exposure")
        if self.volatility_floor >= self.target_annual_volatility:
            raise ValueError("volatility floor must be below the annual target")
        if self.cash_return != 0:
            raise ValueError("the QR-06 cash return must remain zero")
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("volatility targeting must report every required cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> VolatilityTargetConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchVolatilityTargetingError(
                "research_volatility_config_invalid",
                "Research volatility-targeting configuration is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class VolatilityReturnPoint:
    timestamp: datetime
    gross_return: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ResearchVolatilityTargetingError(
                "research_volatility_time_naive", "Return timestamp must be timezone-aware"
            )
        if not self.gross_return.is_finite() or self.gross_return <= Decimal("-1"):
            raise ResearchVolatilityTargetingError(
                "research_volatility_return_invalid", "Gross return must be finite and above -100%"
            )


@dataclass(frozen=True, slots=True)
class VolatilityTargetDecision:
    signal_at: datetime
    execute_at: datetime
    estimated_annual_volatility: Decimal
    unconstrained_exposure: Decimal
    target_exposure: Decimal


@dataclass(frozen=True, slots=True)
class VolatilityTargetMetrics:
    observations: int
    decisions: int
    net_return_pct_by_cost: MappingProxyType[str, Decimal]
    maximum_drawdown_pct_by_cost: MappingProxyType[str, Decimal]
    realized_annual_volatility_by_cost: MappingProxyType[str, Decimal]
    turnover_by_cost: MappingProxyType[str, Decimal]
    static_net_return_pct_by_cost: MappingProxyType[str, Decimal]
    excess_return_pct_vs_static_by_cost: MappingProxyType[str, Decimal]

    @classmethod
    def build(
        cls,
        observations: int,
        decisions: int,
        values: dict[str, dict[str, Decimal]],
    ) -> VolatilityTargetMetrics:
        return cls(
            observations,
            decisions,
            MappingProxyType(values["net"]),
            MappingProxyType(values["drawdown"]),
            MappingProxyType(values["volatility"]),
            MappingProxyType(values["turnover"]),
            MappingProxyType(values["static"]),
            MappingProxyType(values["excess"]),
        )


@dataclass(frozen=True, slots=True)
class VolatilityTargetResult:
    overlay_id: str
    selection_window: Literal["validation"]
    decisions: tuple[VolatilityTargetDecision, ...]
    metrics: VolatilityTargetMetrics
    production_order_routing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class VolatilityTargetOverlay:
    config: VolatilityTargetConfig

    def run(self, points: tuple[VolatilityReturnPoint, ...]) -> VolatilityTargetResult:
        _validate_points(points, self.config)
        decisions = _decisions(points, self.config)
        values: dict[str, dict[str, Decimal]] = {
            name: {} for name in ("net", "drawdown", "volatility", "turnover", "static", "excess")
        }
        for multiplier in self.config.cost_multipliers:
            label = f"{multiplier}x"
            cost_rate = self.config.one_way_cost_bps / Decimal(10000) * multiplier
            curve, period_returns, turnover = _simulate(points, decisions, cost_rate=cost_rate)
            static_curve, _, _ = _simulate_static(
                points,
                exposure=self.config.maximum_exposure,
                cost_rate=cost_rate,
                start_at=decisions[0].execute_at,
            )
            net = (curve[-1] - Decimal(1)) * Decimal(100)
            static = (static_curve[-1] - Decimal(1)) * Decimal(100)
            values["net"][label] = net
            values["drawdown"][label] = _maximum_drawdown(curve) * Decimal(100)
            values["volatility"][label] = _sample_std(period_returns) * _sqrt_decimal(
                Decimal(self.config.annualization_observations)
            )
            values["turnover"][label] = turnover
            values["static"][label] = static
            values["excess"][label] = net - static
        metrics = VolatilityTargetMetrics.build(len(points), len(decisions), values)
        return VolatilityTargetResult(self.config.overlay_id, "validation", decisions, metrics)


def _decisions(
    points: tuple[VolatilityReturnPoint, ...], config: VolatilityTargetConfig
) -> tuple[VolatilityTargetDecision, ...]:
    result: list[VolatilityTargetDecision] = []
    current_exposure = config.minimum_exposure
    annualizer = _sqrt_decimal(Decimal(config.annualization_observations))
    for signal_index in range(config.lookback_observations - 1, len(points) - 1):
        window = points[signal_index - config.lookback_observations + 1 : signal_index + 1]
        estimated = max(
            _sample_std([point.gross_return for point in window]) * annualizer,
            config.volatility_floor,
        )
        unconstrained = config.target_annual_volatility / estimated
        desired = min(
            config.maximum_exposure,
            max(config.minimum_exposure, unconstrained),
        )
        if abs(desired - current_exposure) < config.rebalance_threshold:
            target = current_exposure
        else:
            target = min(
                current_exposure + config.maximum_exposure_step,
                max(current_exposure - config.maximum_exposure_step, desired),
            )
        result.append(
            VolatilityTargetDecision(
                points[signal_index].timestamp,
                points[signal_index + 1].timestamp,
                estimated,
                unconstrained,
                target,
            )
        )
        current_exposure = target
    return tuple(result)


def _simulate(
    points: tuple[VolatilityReturnPoint, ...],
    decisions: tuple[VolatilityTargetDecision, ...],
    *,
    cost_rate: Decimal,
) -> tuple[list[Decimal], list[Decimal], Decimal]:
    by_execution = {decision.execute_at: decision for decision in decisions}
    equity = Decimal(1)
    exposure = Decimal(0)
    curve: list[Decimal] = []
    period_returns: list[Decimal] = []
    turnover = Decimal(0)
    active = False
    for point in points:
        before = equity
        decision = by_execution.get(point.timestamp)
        if decision is not None:
            traded = abs(decision.target_exposure - exposure)
            equity *= Decimal(1) - traded * cost_rate
            turnover += traded
            exposure = decision.target_exposure
            active = True
        equity *= Decimal(1) + exposure * point.gross_return
        curve.append(equity)
        if active:
            period_returns.append(equity / before - Decimal(1))
    equity *= Decimal(1) - exposure * cost_rate
    turnover += exposure
    curve[-1] = equity
    period_returns[-1] = equity / curve[-2] - Decimal(1) if len(curve) > 1 else equity - 1
    return curve, period_returns, turnover


def _simulate_static(
    points: tuple[VolatilityReturnPoint, ...],
    *,
    exposure: Decimal,
    cost_rate: Decimal,
    start_at: datetime,
) -> tuple[list[Decimal], list[Decimal], Decimal]:
    equity = Decimal(1)
    active = False
    curve: list[Decimal] = []
    returns: list[Decimal] = []
    for point in points:
        before = equity
        if point.timestamp == start_at:
            equity *= Decimal(1) - exposure * cost_rate
            active = True
        if active:
            equity *= Decimal(1) + exposure * point.gross_return
        curve.append(equity)
        if active:
            returns.append(equity / before - Decimal(1))
    equity *= Decimal(1) - exposure * cost_rate
    curve[-1] = equity
    returns[-1] = equity / curve[-2] - Decimal(1) if len(curve) > 1 else equity - 1
    return curve, returns, exposure * Decimal(2)


def _sample_std(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    mean = sum(values, Decimal(0)) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)
    return _sqrt_decimal(variance)


def _sqrt_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.sqrt()


def _maximum_drawdown(values: list[Decimal]) -> Decimal:
    peak = values[0]
    maximum = Decimal(0)
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _validate_points(
    points: tuple[VolatilityReturnPoint, ...], config: VolatilityTargetConfig
) -> None:
    if len(points) <= config.lookback_observations:
        raise ResearchVolatilityTargetingError(
            "research_volatility_sample_short", "Return stream is shorter than the required window"
        )
    timestamps = tuple(point.timestamp for point in points)
    if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
        raise ResearchVolatilityTargetingError(
            "research_volatility_order_invalid", "Return timestamps must be unique and increasing"
        )
