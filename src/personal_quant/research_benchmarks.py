"""Cost-aware point-in-time benchmark controls for quantitative research."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchBenchmarkError(ValueError):
    """Benchmark input or evaluation failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


BenchmarkId = Literal[
    "cash",
    "equal_weight_buy_hold",
    "equal_weight_monthly",
    "equal_weight_daily",
]


class BenchmarkSuiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    suite_id: str = Field(min_length=1)
    benchmarks: tuple[BenchmarkId, ...]
    one_way_cost_bps: Decimal = Field(ge=0)
    cost_multipliers: tuple[Decimal, ...]
    minimum_observations: int = Field(ge=2)
    fractional_units: Literal[True]
    selection_window: Literal["validation"]
    production_order_routing: Literal[False]

    @field_validator("benchmarks", mode="before")
    @classmethod
    def freeze_benchmarks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("one_way_cost_bps", "cost_multipliers", mode="before")
    @classmethod
    def parse_decimals(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(Decimal(str(item)) for item in value)
        return Decimal(str(value)) if isinstance(value, (str, int, float)) else value

    @model_validator(mode="after")
    def complete_controls(self) -> BenchmarkSuiteConfig:
        required = {
            "cash",
            "equal_weight_buy_hold",
            "equal_weight_monthly",
            "equal_weight_daily",
        }
        if set(self.benchmarks) != required or len(self.benchmarks) != len(required):
            raise ValueError("benchmark suite must contain each required control exactly once")
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("benchmark suite must report all required cost cases")
        return self

    @classmethod
    def load(cls, path: Path) -> BenchmarkSuiteConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchBenchmarkError(
                "research_benchmark_config_invalid", "Research benchmark configuration is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class BenchmarkPoint:
    """One known-at-time price cross-section and its exact-date eligible membership."""

    timestamp: datetime
    prices: Mapping[str, Decimal]
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ResearchBenchmarkError(
                "research_benchmark_time_naive", "Benchmark timestamps must be timezone-aware"
            )
        if not self.members or len(self.members) != len(set(self.members)):
            raise ResearchBenchmarkError(
                "research_benchmark_members_invalid",
                "Benchmark membership must be non-empty and unique",
            )
        if any(key not in self.prices for key in self.members) or any(
            value <= 0 or not value.is_finite() for value in self.prices.values()
        ):
            raise ResearchBenchmarkError(
                "research_benchmark_prices_invalid", "Benchmark prices are missing or invalid"
            )


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    benchmark_id: BenchmarkId
    observations: int
    net_return_pct_by_cost: Mapping[str, Decimal]
    maximum_drawdown_pct_by_cost: Mapping[str, Decimal]
    turnover_by_cost: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "net_return_pct_by_cost", MappingProxyType(dict(self.net_return_pct_by_cost))
        )
        object.__setattr__(
            self,
            "maximum_drawdown_pct_by_cost",
            MappingProxyType(dict(self.maximum_drawdown_pct_by_cost)),
        )
        object.__setattr__(self, "turnover_by_cost", MappingProxyType(dict(self.turnover_by_cost)))


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteResult:
    suite_id: str
    selection_window: Literal["validation"]
    results: tuple[BenchmarkMetrics, ...]
    production_order_routing: Literal[False] = False

    def result(self, benchmark_id: BenchmarkId) -> BenchmarkMetrics:
        for item in self.results:
            if item.benchmark_id == benchmark_id:
                return item
        raise ResearchBenchmarkError(
            "research_benchmark_missing", "Requested benchmark result does not exist"
        )


@dataclass(frozen=True, slots=True)
class ChallengerComparison:
    strongest_benchmark_by_cost: Mapping[str, str]
    excess_return_pct_by_cost: Mapping[str, Decimal]
    beats_all_cost_cases: bool
    selection_window: Literal["validation"] = "validation"
    eligible_for_operational_promotion: Literal[False] = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "strongest_benchmark_by_cost",
            MappingProxyType(dict(self.strongest_benchmark_by_cost)),
        )
        object.__setattr__(
            self,
            "excess_return_pct_by_cost",
            MappingProxyType(dict(self.excess_return_pct_by_cost)),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    config: BenchmarkSuiteConfig

    def run(self, points: tuple[BenchmarkPoint, ...]) -> BenchmarkSuiteResult:
        _validate_panel(points, self.config.minimum_observations)
        results = tuple(self._run_one(benchmark, points) for benchmark in self.config.benchmarks)
        return BenchmarkSuiteResult(self.config.suite_id, "validation", results)

    def _run_one(
        self, benchmark: BenchmarkId, points: tuple[BenchmarkPoint, ...]
    ) -> BenchmarkMetrics:
        returns: dict[str, Decimal] = {}
        drawdowns: dict[str, Decimal] = {}
        turnovers: dict[str, Decimal] = {}
        for multiplier in self.config.cost_multipliers:
            label = f"{multiplier}x"
            if benchmark == "cash":
                equity, turnover = [Decimal(1)] * len(points), Decimal(0)
            else:
                equity, turnover = _simulate(
                    points,
                    benchmark=benchmark,
                    cost_rate=self.config.one_way_cost_bps / Decimal(10000) * multiplier,
                )
            returns[label] = (equity[-1] - Decimal(1)) * Decimal(100)
            drawdowns[label] = _maximum_drawdown(equity) * Decimal(100)
            turnovers[label] = turnover
        return BenchmarkMetrics(benchmark, len(points), returns, drawdowns, turnovers)


def compare_challenger(
    *, validation_net_return_pct_by_cost: Mapping[str, Decimal], suite: BenchmarkSuiteResult
) -> ChallengerComparison:
    """Compare on validation only; no holdout values are accepted by this API."""
    required = {"1.0x", "1.5x", "2.0x"}
    if set(validation_net_return_pct_by_cost) != required:
        raise ResearchBenchmarkError(
            "research_challenger_costs_missing", "Challenger must report every cost case"
        )
    strongest: dict[str, str] = {}
    excess: dict[str, Decimal] = {}
    for label in sorted(required):
        winner = max(suite.results, key=lambda item: item.net_return_pct_by_cost[label])
        strongest[label] = winner.benchmark_id
        excess[label] = (
            validation_net_return_pct_by_cost[label] - winner.net_return_pct_by_cost[label]
        )
    return ChallengerComparison(strongest, excess, all(value > 0 for value in excess.values()))


def _simulate(
    points: tuple[BenchmarkPoint, ...], *, benchmark: BenchmarkId, cost_rate: Decimal
) -> tuple[list[Decimal], Decimal]:
    cash = Decimal(1)
    units: dict[str, Decimal] = {}
    previous_members: tuple[str, ...] = ()
    equity_curve: list[Decimal] = []
    total_turnover = Decimal(0)
    previous_point: BenchmarkPoint | None = None
    for index, point in enumerate(points):
        if previous_point is not None:
            missing = set(units) - set(point.prices)
            if missing:
                raise ResearchBenchmarkError(
                    "research_benchmark_exit_price_missing",
                    "A held constituent has no price at the membership transition",
                )
        equity = cash + sum(
            (quantity * point.prices[key] for key, quantity in units.items()), Decimal(0)
        )
        membership_changed = point.members != previous_members
        rebalance = (
            index == 0
            or membership_changed
            or benchmark == "equal_weight_daily"
            or (
                benchmark == "equal_weight_monthly"
                and previous_point is not None
                and (point.timestamp.year, point.timestamp.month)
                != (previous_point.timestamp.year, previous_point.timestamp.month)
            )
        )
        if rebalance:
            units, cash, traded = _rebalance(equity, units, point, cost_rate)
            total_turnover += traded
            equity = cash + sum(
                (quantity * point.prices[key] for key, quantity in units.items()), Decimal(0)
            )
        equity_curve.append(equity)
        previous_members = point.members
        previous_point = point
    final = points[-1]
    proceeds = sum((quantity * final.prices[key] for key, quantity in units.items()), Decimal(0))
    liquidation_cost = proceeds * cost_rate
    total_turnover += proceeds
    equity_curve[-1] = cash + proceeds - liquidation_cost
    return equity_curve, total_turnover


def _rebalance(
    equity: Decimal,
    current_units: dict[str, Decimal],
    point: BenchmarkPoint,
    cost_rate: Decimal,
) -> tuple[dict[str, Decimal], Decimal, Decimal]:
    investable = equity
    traded = Decimal(0)
    targets: dict[str, Decimal] = {}
    for _ in range(4):
        target_value = investable / len(point.members)
        targets = {key: target_value / point.prices[key] for key in point.members}
        traded = sum(
            (
                abs(targets.get(key, Decimal(0)) * point.prices[key] - quantity * point.prices[key])
                for key, quantity in current_units.items()
            ),
            Decimal(0),
        ) + sum(
            (targets[key] * point.prices[key] for key in point.members if key not in current_units),
            Decimal(0),
        )
        investable = equity - traded * cost_rate
    if investable < 0:
        raise ResearchBenchmarkError(
            "research_benchmark_cost_exhausted", "Transaction costs exhaust benchmark capital"
        )
    fee = traded * cost_rate
    invested = sum((targets[key] * point.prices[key] for key in targets), Decimal(0))
    return targets, equity - invested - fee, traded


def _validate_panel(points: tuple[BenchmarkPoint, ...], minimum: int) -> None:
    if len(points) < minimum:
        raise ResearchBenchmarkError(
            "research_benchmark_sample_short", "Benchmark panel has too few observations"
        )
    timestamps = tuple(point.timestamp for point in points)
    if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
        raise ResearchBenchmarkError(
            "research_benchmark_order_invalid", "Benchmark timestamps must be unique and increasing"
        )


def _maximum_drawdown(equity: list[Decimal]) -> Decimal:
    peak = equity[0]
    maximum = Decimal(0)
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum
