"""Leakage-safe, cost-aware cross-sectional momentum research challenger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_benchmarks import (
    BenchmarkPoint,
    BenchmarkSuiteResult,
    ChallengerComparison,
    compare_challenger,
)


class ResearchMomentumError(ValueError):
    """Momentum configuration or evaluation failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MomentumConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    strategy_id: str = Field(min_length=1)
    lookback_observations: int = Field(ge=3)
    skip_recent_observations: int = Field(ge=1)
    minimum_universe: int = Field(ge=2)
    top_fraction: Decimal = Field(gt=0, le=1)
    maximum_positions: int = Field(ge=1)
    rank_buffer: int = Field(ge=0)
    maximum_weight: Decimal = Field(gt=0, le=1)
    volatility_floor: Decimal = Field(gt=0)
    one_way_cost_bps: Decimal = Field(ge=0)
    cost_multipliers: tuple[Decimal, ...]
    rebalance: Literal["month_end"]
    signal_execution_lag_observations: Literal[1]
    selection_window: Literal["validation"]
    long_only: Literal[True]
    fractional_units: Literal[True]
    production_order_routing: Literal[False]

    @field_validator(
        "top_fraction",
        "maximum_weight",
        "volatility_floor",
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
    def complete_contract(self) -> MomentumConfig:
        if self.skip_recent_observations >= self.lookback_observations:
            raise ValueError("momentum skip must be shorter than its lookback")
        if self.maximum_positions > self.minimum_universe:
            raise ValueError("maximum positions cannot exceed the minimum universe")
        if self.maximum_weight * self.maximum_positions < Decimal(1):
            raise ValueError("maximum weight makes full investment impossible")
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("momentum must report every required cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> MomentumConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchMomentumError(
                "research_momentum_config_invalid", "Research momentum configuration is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class MomentumDecision:
    signal_at: datetime
    execute_at: datetime
    ranked_instruments: tuple[str, ...]
    selected_instruments: tuple[str, ...]
    target_weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_weights", MappingProxyType(dict(self.target_weights)))


@dataclass(frozen=True, slots=True)
class MomentumMetrics:
    observations: int
    rebalance_count: int
    net_return_pct_by_cost: Mapping[str, Decimal]
    maximum_drawdown_pct_by_cost: Mapping[str, Decimal]
    turnover_by_cost: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        for name in (
            "net_return_pct_by_cost",
            "maximum_drawdown_pct_by_cost",
            "turnover_by_cost",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class MomentumResult:
    strategy_id: str
    selection_window: Literal["validation"]
    decisions: tuple[MomentumDecision, ...]
    metrics: MomentumMetrics
    production_order_routing: Literal[False] = False

    def compare_to(self, benchmarks: BenchmarkSuiteResult) -> ChallengerComparison:
        return compare_challenger(
            validation_net_return_pct_by_cost=self.metrics.net_return_pct_by_cost,
            suite=benchmarks,
        )


@dataclass(frozen=True, slots=True)
class MomentumChallenger:
    config: MomentumConfig

    def run(self, points: tuple[BenchmarkPoint, ...]) -> MomentumResult:
        _validate_points(points, self.config)
        decisions = _decisions(points, self.config)
        returns: dict[str, Decimal] = {}
        drawdowns: dict[str, Decimal] = {}
        turnovers: dict[str, Decimal] = {}
        for multiplier in self.config.cost_multipliers:
            label = f"{multiplier}x"
            equity, turnover = _simulate(
                points,
                decisions,
                cost_rate=self.config.one_way_cost_bps / Decimal(10000) * multiplier,
            )
            returns[label] = (equity[-1] - Decimal(1)) * Decimal(100)
            drawdowns[label] = _maximum_drawdown(equity) * Decimal(100)
            turnovers[label] = turnover
        metrics = MomentumMetrics(len(points), len(decisions), returns, drawdowns, turnovers)
        return MomentumResult(self.config.strategy_id, "validation", decisions, metrics)


def _decisions(
    points: tuple[BenchmarkPoint, ...], config: MomentumConfig
) -> tuple[MomentumDecision, ...]:
    result: list[MomentumDecision] = []
    held: tuple[str, ...] = ()
    for signal_index in range(config.lookback_observations - 1, len(points) - 1):
        signal_point = points[signal_index]
        execution_point = points[signal_index + 1]
        if (signal_point.timestamp.year, signal_point.timestamp.month) == (
            execution_point.timestamp.year,
            execution_point.timestamp.month,
        ):
            continue
        score_end = signal_index - config.skip_recent_observations
        score_start = signal_index - config.lookback_observations + 1
        score_window = points[score_start : score_end + 1]
        eligibility_window = points[score_start : signal_index + 1]
        eligible = _eligible(eligibility_window, signal_point.members)
        if len(eligible) < config.minimum_universe:
            continue
        scores = {
            key: score_window[-1].prices[key] / score_window[0].prices[key] - Decimal(1)
            for key in eligible
        }
        ranked = tuple(sorted(eligible, key=lambda key: (-scores[key], key)))
        target_count = min(
            config.maximum_positions,
            max(
                1,
                int(
                    (Decimal(len(ranked)) * config.top_fraction).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                ),
            ),
        )
        buffered = set(ranked[: target_count + config.rank_buffer])
        selected = [key for key in held if key in buffered]
        selected.extend(key for key in ranked if key not in selected)
        chosen = tuple(selected[:target_count])
        weights = _inverse_volatility_weights(score_window, chosen, config)
        result.append(
            MomentumDecision(
                signal_at=signal_point.timestamp,
                execute_at=execution_point.timestamp,
                ranked_instruments=ranked,
                selected_instruments=chosen,
                target_weights=weights,
            )
        )
        held = chosen
    return tuple(result)


def _eligible(window: tuple[BenchmarkPoint, ...], current: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        key
        for key in current
        if all(key in point.members and key in point.prices for point in window)
    )


def _inverse_volatility_weights(
    window: tuple[BenchmarkPoint, ...], selected: tuple[str, ...], config: MomentumConfig
) -> Mapping[str, Decimal]:
    inverse: dict[str, Decimal] = {}
    for key in selected:
        returns = [
            window[index].prices[key] / window[index - 1].prices[key] - Decimal(1)
            for index in range(1, len(window))
        ]
        volatility = _sample_std(returns)
        inverse[key] = Decimal(1) / max(volatility, config.volatility_floor)
    total = sum(inverse.values(), Decimal(0))
    raw = {key: value / total for key, value in inverse.items()}
    return MappingProxyType(_cap_weights(raw, config.maximum_weight))


def _cap_weights(weights: dict[str, Decimal], maximum: Decimal) -> dict[str, Decimal]:
    result = dict(weights)
    uncapped = set(result)
    while uncapped:
        capped = {key for key in uncapped if result[key] > maximum}
        if not capped:
            break
        for key in capped:
            result[key] = maximum
        uncapped -= capped
        remaining = Decimal(1) - sum(
            (value for key, value in result.items() if key not in uncapped), Decimal(0)
        )
        base = sum((weights[key] for key in uncapped), Decimal(0))
        if not uncapped or base <= 0:
            break
        for key in uncapped:
            result[key] = remaining * weights[key] / base
    return result


def _simulate(
    points: tuple[BenchmarkPoint, ...],
    decisions: tuple[MomentumDecision, ...],
    *,
    cost_rate: Decimal,
) -> tuple[list[Decimal], Decimal]:
    by_execution = {decision.execute_at: decision for decision in decisions}
    cash = Decimal(1)
    units: dict[str, Decimal] = {}
    equity_curve: list[Decimal] = []
    turnover = Decimal(0)
    for point in points:
        missing = set(units) - set(point.prices)
        if missing:
            raise ResearchMomentumError(
                "research_momentum_exit_price_missing", "A held instrument has no exit price"
            )
        ineligible = set(units) - set(point.members)
        for key in ineligible:
            proceeds = units.pop(key) * point.prices[key]
            cash += proceeds - proceeds * cost_rate
            turnover += proceeds
        equity = cash + sum(
            (quantity * point.prices[key] for key, quantity in units.items()), Decimal(0)
        )
        decision = by_execution.get(point.timestamp)
        if decision is not None:
            if set(decision.target_weights) - set(point.prices) or set(
                decision.target_weights
            ) - set(point.members):
                raise ResearchMomentumError(
                    "research_momentum_entry_price_missing",
                    "A selected instrument has no next-observation execution price",
                )
            units, cash, traded = _rebalance(
                equity, units, decision.target_weights, point.prices, cost_rate
            )
            turnover += traded
            equity = cash + sum(
                (quantity * point.prices[key] for key, quantity in units.items()), Decimal(0)
            )
        equity_curve.append(equity)
    final = points[-1]
    proceeds = sum((quantity * final.prices[key] for key, quantity in units.items()), Decimal(0))
    turnover += proceeds
    equity_curve[-1] = cash + proceeds - proceeds * cost_rate
    return equity_curve, turnover


def _rebalance(
    equity: Decimal,
    current_units: dict[str, Decimal],
    weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    cost_rate: Decimal,
) -> tuple[dict[str, Decimal], Decimal, Decimal]:
    investable = equity
    targets: dict[str, Decimal] = {}
    traded = Decimal(0)
    for _ in range(4):
        targets = {key: investable * weight / prices[key] for key, weight in weights.items()}
        keys = set(current_units) | set(targets)
        traded = sum(
            (
                abs(
                    targets.get(key, Decimal(0)) * prices[key]
                    - current_units.get(key, Decimal(0)) * prices[key]
                )
                for key in keys
            ),
            Decimal(0),
        )
        investable = equity - traded * cost_rate
    if investable < 0:
        raise ResearchMomentumError(
            "research_momentum_cost_exhausted", "Transaction costs exhaust momentum capital"
        )
    invested = sum((targets[key] * prices[key] for key in targets), Decimal(0))
    return targets, equity - invested - traded * cost_rate, traded


def _sample_std(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    mean = sum(values, Decimal(0)) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)
    with localcontext() as context:
        context.prec = 28
        return variance.sqrt()


def _maximum_drawdown(equity: list[Decimal]) -> Decimal:
    peak = equity[0]
    maximum = Decimal(0)
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _validate_points(points: tuple[BenchmarkPoint, ...], config: MomentumConfig) -> None:
    if len(points) <= config.lookback_observations:
        raise ResearchMomentumError(
            "research_momentum_sample_short", "Momentum panel is shorter than its lookback"
        )
    timestamps = tuple(point.timestamp for point in points)
    if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
        raise ResearchMomentumError(
            "research_momentum_order_invalid", "Momentum timestamps must be unique and increasing"
        )
