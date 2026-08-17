"""Correlation-aware allocation across validated research strategy streams."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchPortfolioAllocationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class PortfolioAllocationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    allocator_id: str = Field(min_length=1)
    lookback_observations: int = Field(ge=5)
    minimum_strategies: int = Field(ge=2)
    volatility_floor: Decimal = Field(gt=0)
    correlation_penalty: Decimal = Field(ge=0)
    maximum_strategy_weight: Decimal = Field(gt=0, le=1)
    rebalance_threshold: Decimal = Field(ge=0, le=1)
    maximum_one_way_turnover: Decimal = Field(gt=0, le=1)
    one_way_cost_bps: Decimal = Field(ge=0)
    cost_multipliers: tuple[Decimal, ...]
    signal_execution_lag_observations: Literal[1]
    selection_window: Literal["validation"]
    long_only: Literal[True]
    cash_allowed: Literal[True]
    production_order_routing: Literal[False]

    @field_validator(
        "volatility_floor",
        "correlation_penalty",
        "maximum_strategy_weight",
        "rebalance_threshold",
        "maximum_one_way_turnover",
        "one_way_cost_bps",
        "cost_multipliers",
        mode="before",
    )
    @classmethod
    def parse_decimals(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(Decimal(str(item)) for item in value)
        return Decimal(str(value)) if isinstance(value, (str, int, float)) else value

    @model_validator(mode="after")
    def complete_contract(self) -> PortfolioAllocationConfig:
        if self.maximum_strategy_weight * self.minimum_strategies < 1:
            raise ValueError("strategy cap cannot fund a complete minimum-size portfolio")
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("portfolio allocation must report every required cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> PortfolioAllocationConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchPortfolioAllocationError(
                "research_allocation_config_invalid",
                "Research portfolio-allocation configuration is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class AllocationPoint:
    timestamp: datetime
    strategy_returns: Mapping[str, Decimal]
    available_strategies: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_returns", MappingProxyType(dict(self.strategy_returns)))
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ResearchPortfolioAllocationError(
                "research_allocation_time_naive", "Allocation timestamp must be timezone-aware"
            )
        if not self.available_strategies or len(self.available_strategies) != len(
            set(self.available_strategies)
        ):
            raise ResearchPortfolioAllocationError(
                "research_allocation_members_invalid", "Strategy availability is invalid"
            )
        if any(
            key not in self.strategy_returns
            or not self.strategy_returns[key].is_finite()
            or self.strategy_returns[key] <= Decimal("-1")
            for key in self.available_strategies
        ):
            raise ResearchPortfolioAllocationError(
                "research_allocation_returns_invalid", "Strategy returns are invalid"
            )


@dataclass(frozen=True, slots=True)
class PortfolioAllocationDecision:
    signal_at: datetime
    execute_at: datetime
    eligible_strategies: tuple[str, ...]
    estimated_volatility: Mapping[str, Decimal]
    estimated_correlation: Mapping[str, Mapping[str, Decimal]]
    unconstrained_weights: Mapping[str, Decimal]
    target_weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "estimated_volatility", MappingProxyType(dict(self.estimated_volatility))
        )
        object.__setattr__(
            self,
            "estimated_correlation",
            MappingProxyType(
                {
                    key: MappingProxyType(dict(values))
                    for key, values in self.estimated_correlation.items()
                }
            ),
        )
        object.__setattr__(
            self, "unconstrained_weights", MappingProxyType(dict(self.unconstrained_weights))
        )
        object.__setattr__(self, "target_weights", MappingProxyType(dict(self.target_weights)))


@dataclass(frozen=True, slots=True)
class PortfolioAllocationMetrics:
    observations: int
    decisions: int
    net_return_pct_by_cost: Mapping[str, Decimal]
    maximum_drawdown_pct_by_cost: Mapping[str, Decimal]
    turnover_by_cost: Mapping[str, Decimal]
    equal_weight_net_return_pct_by_cost: Mapping[str, Decimal]
    excess_return_pct_vs_equal_weight_by_cost: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        for name in (
            "net_return_pct_by_cost",
            "maximum_drawdown_pct_by_cost",
            "turnover_by_cost",
            "equal_weight_net_return_pct_by_cost",
            "excess_return_pct_vs_equal_weight_by_cost",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class PortfolioAllocationResult:
    allocator_id: str
    selection_window: Literal["validation"]
    decisions: tuple[PortfolioAllocationDecision, ...]
    metrics: PortfolioAllocationMetrics
    production_order_routing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class CorrelationAwareAllocator:
    config: PortfolioAllocationConfig

    def run(self, points: tuple[AllocationPoint, ...]) -> PortfolioAllocationResult:
        _validate_points(points, self.config)
        decisions = _decisions(points, self.config)
        returns: dict[str, Decimal] = {}
        drawdowns: dict[str, Decimal] = {}
        turnovers: dict[str, Decimal] = {}
        controls: dict[str, Decimal] = {}
        excess: dict[str, Decimal] = {}
        for multiplier in self.config.cost_multipliers:
            label = f"{multiplier}x"
            cost_rate = self.config.one_way_cost_bps / Decimal(10000) * multiplier
            curve, turnover = _simulate(points, decisions, cost_rate=cost_rate)
            control_curve, _ = _simulate(
                points,
                _equal_weight_decisions(decisions, self.config),
                cost_rate=cost_rate,
            )
            net = (curve[-1] - Decimal(1)) * Decimal(100)
            control = (control_curve[-1] - Decimal(1)) * Decimal(100)
            returns[label] = net
            drawdowns[label] = _maximum_drawdown(curve) * Decimal(100)
            turnovers[label] = turnover
            controls[label] = control
            excess[label] = net - control
        metrics = PortfolioAllocationMetrics(
            len(points),
            len(decisions),
            returns,
            drawdowns,
            turnovers,
            controls,
            excess,
        )
        return PortfolioAllocationResult(self.config.allocator_id, "validation", decisions, metrics)


def _decisions(
    points: tuple[AllocationPoint, ...], config: PortfolioAllocationConfig
) -> tuple[PortfolioAllocationDecision, ...]:
    result: list[PortfolioAllocationDecision] = []
    current: dict[str, Decimal] = {}
    for signal_index in range(config.lookback_observations - 1, len(points) - 1):
        window = points[signal_index - config.lookback_observations + 1 : signal_index + 1]
        eligible = tuple(
            key
            for key in points[signal_index].available_strategies
            if all(
                key in point.available_strategies and key in point.strategy_returns
                for point in window
            )
        )
        volatility: dict[str, Decimal] = {}
        correlation: dict[str, dict[str, Decimal]] = {}
        unconstrained: dict[str, Decimal] = {}
        desired: dict[str, Decimal] = {}
        if len(eligible) >= config.minimum_strategies:
            series = {key: [point.strategy_returns[key] for point in window] for key in eligible}
            volatility = {
                key: max(_sample_std(values), config.volatility_floor)
                for key, values in series.items()
            }
            correlation = {
                left: {right: _correlation(series[left], series[right]) for right in eligible}
                for left in eligible
            }
            scores = {
                key: Decimal(1)
                / (
                    volatility[key]
                    * (
                        Decimal(1)
                        + config.correlation_penalty
                        * sum(
                            (
                                max(correlation[key][other], Decimal(0))
                                for other in eligible
                                if other != key
                            ),
                            Decimal(0),
                        )
                        / (len(eligible) - 1)
                    )
                )
                for key in eligible
            }
            score_total = sum(scores.values(), Decimal(0))
            unconstrained = {key: score / score_total for key, score in scores.items()}
            desired = _cap_weights(unconstrained, config.maximum_strategy_weight)
        target = _transition_weights(
            current,
            desired,
            eligible=set(eligible),
            threshold=config.rebalance_threshold,
            turnover_limit=config.maximum_one_way_turnover,
        )
        result.append(
            PortfolioAllocationDecision(
                points[signal_index].timestamp,
                points[signal_index + 1].timestamp,
                eligible,
                volatility,
                correlation,
                unconstrained,
                target,
            )
        )
        current = target
    return tuple(result)


def _cap_weights(weights: Mapping[str, Decimal], cap: Decimal) -> dict[str, Decimal]:
    remaining = set(weights)
    allocated: dict[str, Decimal] = {}
    remaining_mass = Decimal(1)
    while remaining:
        score_total = sum((weights[key] for key in remaining), Decimal(0))
        proposed = {key: remaining_mass * weights[key] / score_total for key in remaining}
        capped = {key for key, value in proposed.items() if value > cap}
        if not capped:
            allocated.update(proposed)
            break
        for key in capped:
            allocated[key] = cap
            remaining_mass -= cap
            remaining.remove(key)
    return allocated


def _transition_weights(
    current: Mapping[str, Decimal],
    desired: Mapping[str, Decimal],
    *,
    eligible: set[str],
    threshold: Decimal,
    turnover_limit: Decimal,
) -> dict[str, Decimal]:
    forced = sum((weight for key, weight in current.items() if key not in eligible), Decimal(0))
    base = {key: weight for key, weight in current.items() if key in eligible}
    keys = set(base) | set(desired)
    discretionary = sum(
        (abs(desired.get(key, Decimal(0)) - base.get(key, Decimal(0))) for key in keys),
        Decimal(0),
    )
    if forced == 0 and discretionary < threshold:
        return dict(current)
    allowed = max(Decimal(0), turnover_limit - forced)
    ratio = min(Decimal(1), allowed / discretionary) if discretionary > 0 else Decimal(0)
    return {
        key: base.get(key, Decimal(0))
        + ratio * (desired.get(key, Decimal(0)) - base.get(key, Decimal(0)))
        for key in keys
        if base.get(key, Decimal(0))
        + ratio * (desired.get(key, Decimal(0)) - base.get(key, Decimal(0)))
        > 0
    }


def _equal_weight_decisions(
    decisions: tuple[PortfolioAllocationDecision, ...],
    config: PortfolioAllocationConfig,
) -> tuple[PortfolioAllocationDecision, ...]:
    result: list[PortfolioAllocationDecision] = []
    current: dict[str, Decimal] = {}
    for decision in decisions:
        weight = (
            Decimal(1) / len(decision.eligible_strategies)
            if decision.eligible_strategies
            else Decimal(0)
        )
        desired = {key: weight for key in decision.eligible_strategies}
        target = _transition_weights(
            current,
            desired,
            eligible=set(decision.eligible_strategies),
            threshold=config.rebalance_threshold,
            turnover_limit=config.maximum_one_way_turnover,
        )
        result.append(
            PortfolioAllocationDecision(
                decision.signal_at,
                decision.execute_at,
                decision.eligible_strategies,
                decision.estimated_volatility,
                decision.estimated_correlation,
                desired,
                target,
            )
        )
        current = target
    return tuple(result)


def _simulate(
    points: tuple[AllocationPoint, ...],
    decisions: tuple[PortfolioAllocationDecision, ...],
    *,
    cost_rate: Decimal,
) -> tuple[list[Decimal], Decimal]:
    by_execution = {decision.execute_at: decision for decision in decisions}
    weights: dict[str, Decimal] = {}
    equity = Decimal(1)
    turnover = Decimal(0)
    curve: list[Decimal] = []
    for point in points:
        decision = by_execution.get(point.timestamp)
        if decision is not None:
            if set(decision.target_weights) - set(point.available_strategies):
                raise ResearchPortfolioAllocationError(
                    "research_allocation_execution_unavailable",
                    "A target strategy is unavailable at execution",
                )
            traded = sum(
                (
                    abs(decision.target_weights.get(key, Decimal(0)) - weights.get(key, Decimal(0)))
                    for key in set(weights) | set(decision.target_weights)
                ),
                Decimal(0),
            )
            equity *= Decimal(1) - traded * cost_rate
            turnover += traded
            weights = dict(decision.target_weights)
        if set(weights) - set(point.strategy_returns):
            raise ResearchPortfolioAllocationError(
                "research_allocation_return_missing", "A held strategy return is missing"
            )
        portfolio_return = sum(
            (weight * point.strategy_returns[key] for key, weight in weights.items()), Decimal(0)
        )
        equity *= Decimal(1) + portfolio_return
        curve.append(equity)
    liquidation = sum(weights.values(), Decimal(0))
    equity *= Decimal(1) - liquidation * cost_rate
    turnover += liquidation
    curve[-1] = equity
    return curve, turnover


def _sample_std(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    return _sqrt_decimal(_covariance(values, values))


def _correlation(left: list[Decimal], right: list[Decimal]) -> Decimal:
    left_std = _sample_std(left)
    right_std = _sample_std(right)
    if left_std == 0 or right_std == 0:
        return Decimal(0)
    return _covariance(left, right) / (left_std * right_std)


def _covariance(left: list[Decimal], right: list[Decimal]) -> Decimal:
    left_mean = sum(left, Decimal(0)) / len(left)
    right_mean = sum(right, Decimal(0)) / len(right)
    return sum(
        (
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left, right, strict=True)
        ),
        Decimal(0),
    ) / (len(left) - 1)


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
    points: tuple[AllocationPoint, ...], config: PortfolioAllocationConfig
) -> None:
    if len(points) <= config.lookback_observations:
        raise ResearchPortfolioAllocationError(
            "research_allocation_sample_short", "Strategy panel is shorter than the required window"
        )
    timestamps = tuple(point.timestamp for point in points)
    if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
        raise ResearchPortfolioAllocationError(
            "research_allocation_order_invalid",
            "Allocation timestamps must be unique and increasing",
        )
