"""Regime-aware cross-sectional mean-reversion research challenger."""

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
    BenchmarkSuiteResult,
    ChallengerComparison,
    compare_challenger,
)


class ResearchMeanReversionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MeanReversionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    strategy_id: str = Field(min_length=1)
    signal_lookback_observations: int = Field(ge=2)
    regime_lookback_observations: int = Field(ge=5)
    minimum_universe: int = Field(ge=3)
    entry_zscore: Decimal = Field(lt=0)
    exit_zscore: Decimal = Field(le=0)
    bottom_fraction: Decimal = Field(gt=0, le=1)
    maximum_positions: int = Field(ge=1)
    maximum_weight: Decimal = Field(gt=0, le=1)
    minimum_dollar_volume: Decimal = Field(gt=0)
    trend_threshold: Decimal = Field(gt=0)
    volatility_threshold: Decimal = Field(gt=0)
    one_way_cost_bps: Decimal = Field(ge=0)
    cost_multipliers: tuple[Decimal, ...]
    signal_execution_lag_observations: Literal[1]
    selection_window: Literal["validation"]
    long_only: Literal[True]
    fractional_units: Literal[True]
    production_order_routing: Literal[False]

    @field_validator(
        "entry_zscore",
        "exit_zscore",
        "bottom_fraction",
        "maximum_weight",
        "minimum_dollar_volume",
        "trend_threshold",
        "volatility_threshold",
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
    def complete_contract(self) -> MeanReversionConfig:
        if self.entry_zscore >= self.exit_zscore:
            raise ValueError("entry z-score must be below exit z-score")
        if self.maximum_positions > self.minimum_universe:
            raise ValueError("maximum positions cannot exceed minimum universe")
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("mean reversion must report every required cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> MeanReversionConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchMeanReversionError(
                "research_mean_reversion_config_invalid",
                "Research mean-reversion configuration is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class MeanReversionPoint:
    timestamp: datetime
    prices: Mapping[str, Decimal]
    dollar_volumes: Mapping[str, Decimal]
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))
        object.__setattr__(self, "dollar_volumes", MappingProxyType(dict(self.dollar_volumes)))
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ResearchMeanReversionError(
                "research_mean_reversion_time_naive", "Point timestamp must be timezone-aware"
            )
        if not self.members or len(self.members) != len(set(self.members)):
            raise ResearchMeanReversionError(
                "research_mean_reversion_members_invalid", "Point membership is invalid"
            )
        if any(
            key not in self.prices
            or key not in self.dollar_volumes
            or self.prices[key] <= 0
            or self.dollar_volumes[key] <= 0
            for key in self.members
        ):
            raise ResearchMeanReversionError(
                "research_mean_reversion_data_invalid", "Prices or dollar volumes are invalid"
            )


Regime = Literal["RANGE_NORMAL", "TRENDING", "HIGH_VOLATILITY"]


@dataclass(frozen=True, slots=True)
class MeanReversionDecision:
    signal_at: datetime
    execute_at: datetime
    regime: Regime
    market_trend: Decimal
    market_volatility: Decimal
    selected_instruments: tuple[str, ...]
    target_weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_weights", MappingProxyType(dict(self.target_weights)))


@dataclass(frozen=True, slots=True)
class MeanReversionMetrics:
    observations: int
    active_decisions: int
    blocked_decisions: int
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
class MeanReversionResult:
    strategy_id: str
    selection_window: Literal["validation"]
    decisions: tuple[MeanReversionDecision, ...]
    metrics: MeanReversionMetrics
    production_order_routing: Literal[False] = False

    def compare_to(self, benchmarks: BenchmarkSuiteResult) -> ChallengerComparison:
        return compare_challenger(
            validation_net_return_pct_by_cost=self.metrics.net_return_pct_by_cost,
            suite=benchmarks,
        )


@dataclass(frozen=True, slots=True)
class MeanReversionChallenger:
    config: MeanReversionConfig

    def run(self, points: tuple[MeanReversionPoint, ...]) -> MeanReversionResult:
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
        active = sum(decision.regime == "RANGE_NORMAL" for decision in decisions)
        metrics = MeanReversionMetrics(
            len(points), active, len(decisions) - active, returns, drawdowns, turnovers
        )
        return MeanReversionResult(self.config.strategy_id, "validation", decisions, metrics)


def _decisions(
    points: tuple[MeanReversionPoint, ...], config: MeanReversionConfig
) -> tuple[MeanReversionDecision, ...]:
    start = max(config.signal_lookback_observations, config.regime_lookback_observations)
    held: tuple[str, ...] = ()
    result: list[MeanReversionDecision] = []
    for signal_index in range(start, len(points) - 1):
        signal_window = points[
            signal_index - config.signal_lookback_observations : signal_index + 1
        ]
        regime_window = points[
            signal_index - config.regime_lookback_observations : signal_index + 1
        ]
        current = points[signal_index]
        execution = points[signal_index + 1]
        eligible = tuple(
            key
            for key in current.members
            if current.dollar_volumes[key] >= config.minimum_dollar_volume
            and all(key in point.members and key in point.prices for point in signal_window)
            and all(key in point.members and key in point.prices for point in regime_window)
        )
        if len(eligible) < config.minimum_universe:
            continue
        market_returns = [
            sum(
                (
                    regime_window[index].prices[key] / regime_window[index - 1].prices[key]
                    - Decimal(1)
                    for key in eligible
                ),
                Decimal(0),
            )
            / len(eligible)
            for index in range(1, len(regime_window))
        ]
        trend = sum(
            (
                regime_window[-1].prices[key] / regime_window[0].prices[key] - Decimal(1)
                for key in eligible
            ),
            Decimal(0),
        ) / len(eligible)
        volatility = _sample_std(market_returns)
        regime: Regime = "RANGE_NORMAL"
        if volatility >= config.volatility_threshold:
            regime = "HIGH_VOLATILITY"
        elif abs(trend) >= config.trend_threshold:
            regime = "TRENDING"
        selected: tuple[str, ...] = ()
        weights: Mapping[str, Decimal] = MappingProxyType({})
        if regime == "RANGE_NORMAL":
            reversals = {
                key: signal_window[-1].prices[key] / signal_window[0].prices[key] - Decimal(1)
                for key in eligible
            }
            mean = sum(reversals.values(), Decimal(0)) / len(reversals)
            dispersion = _sample_std(list(reversals.values()))
            zscores = {
                key: (value - mean) / dispersion if dispersion > 0 else Decimal(0)
                for key, value in reversals.items()
            }
            ranked = tuple(sorted(eligible, key=lambda key: (zscores[key], key)))
            count = min(
                config.maximum_positions,
                max(
                    1,
                    int(
                        (Decimal(len(ranked)) * config.bottom_fraction).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ),
                ),
            )
            retained = [
                key for key in held if key in zscores and zscores[key] <= config.exit_zscore
            ]
            entrants = [key for key in ranked if zscores[key] <= config.entry_zscore]
            retained.extend(key for key in entrants if key not in retained)
            selected = tuple(retained[:count])
            weights = _equal_capped_weights(selected, config.maximum_weight)
        result.append(
            MeanReversionDecision(
                current.timestamp,
                execution.timestamp,
                regime,
                trend,
                volatility,
                selected,
                weights,
            )
        )
        held = selected
    return tuple(result)


def _equal_capped_weights(selected: tuple[str, ...], maximum: Decimal) -> Mapping[str, Decimal]:
    if not selected:
        return MappingProxyType({})
    weight = min(Decimal(1) / len(selected), maximum)
    return MappingProxyType({key: weight for key in selected})


def _simulate(
    points: tuple[MeanReversionPoint, ...],
    decisions: tuple[MeanReversionDecision, ...],
    *,
    cost_rate: Decimal,
) -> tuple[list[Decimal], Decimal]:
    by_execution = {decision.execute_at: decision for decision in decisions}
    cash = Decimal(1)
    units: dict[str, Decimal] = {}
    curve: list[Decimal] = []
    turnover = Decimal(0)
    for point in points:
        if set(units) - set(point.prices):
            raise ResearchMeanReversionError(
                "research_mean_reversion_exit_price_missing", "A held instrument has no exit price"
            )
        for key in set(units) - set(point.members):
            proceeds = units.pop(key) * point.prices[key]
            cash += proceeds - proceeds * cost_rate
            turnover += proceeds
        equity = cash + sum(
            (quantity * point.prices[key] for key, quantity in units.items()), Decimal(0)
        )
        decision = by_execution.get(point.timestamp)
        if decision is not None:
            if set(decision.target_weights) - set(point.members) or set(
                decision.target_weights
            ) - set(point.prices):
                raise ResearchMeanReversionError(
                    "research_mean_reversion_entry_price_missing",
                    "A selected instrument is unavailable at execution",
                )
            units, cash, traded = _rebalance(
                equity, units, decision.target_weights, point.prices, cost_rate
            )
            turnover += traded
            equity = cash + sum(
                (quantity * point.prices[key] for key, quantity in units.items()), Decimal(0)
            )
        curve.append(equity)
    final = points[-1]
    proceeds = sum((quantity * final.prices[key] for key, quantity in units.items()), Decimal(0))
    turnover += proceeds
    curve[-1] = cash + proceeds - proceeds * cost_rate
    return curve, turnover


def _rebalance(
    equity: Decimal,
    current: dict[str, Decimal],
    weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    cost_rate: Decimal,
) -> tuple[dict[str, Decimal], Decimal, Decimal]:
    investable = equity
    targets: dict[str, Decimal] = {}
    traded = Decimal(0)
    for _ in range(4):
        targets = {key: investable * weight / prices[key] for key, weight in weights.items()}
        traded = sum(
            (
                abs(
                    targets.get(key, Decimal(0)) * prices[key]
                    - current.get(key, Decimal(0)) * prices[key]
                )
                for key in set(current) | set(targets)
            ),
            Decimal(0),
        )
        investable = equity - traded * cost_rate
    if investable < 0:
        raise ResearchMeanReversionError(
            "research_mean_reversion_cost_exhausted", "Costs exhaust mean-reversion capital"
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


def _maximum_drawdown(values: list[Decimal]) -> Decimal:
    peak = values[0]
    maximum = Decimal(0)
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _validate_points(points: tuple[MeanReversionPoint, ...], config: MeanReversionConfig) -> None:
    if len(points) <= max(config.signal_lookback_observations, config.regime_lookback_observations):
        raise ResearchMeanReversionError(
            "research_mean_reversion_sample_short", "Panel is shorter than required lookbacks"
        )
    timestamps = tuple(point.timestamp for point in points)
    if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
        raise ResearchMeanReversionError(
            "research_mean_reversion_order_invalid",
            "Point timestamps must be unique and increasing",
        )
