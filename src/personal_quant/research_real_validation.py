"""Real historical feature assembly and controlled validation execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_quant.research_candidate_freeze import (
    CandidateDossier,
    CandidateFreezeGate,
    CandidateProvenance,
)
from personal_quant.research_ml_dataset import (
    MLDatasetConfig,
    MLFeaturePoint,
    PurgedWalkForwardDatasetBuilder,
)
from personal_quant.research_model_evaluation import (
    ModelEvaluationResult,
    ModelEvaluationWorkflow,
)
from personal_quant.research_universe import PointInTimeUniverse, ResearchUniverseError


class ResearchRealValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RealValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    runner_id: str = Field(min_length=1)
    momentum_lookback_observations: int = Field(ge=2)
    reversal_lookback_observations: int = Field(ge=1)
    volatility_lookback_observations: int = Field(ge=2)
    minimum_instruments_per_observation: int = Field(ge=2)
    required_price_adjustment: Literal["corporate_action_adjusted"]
    required_interval: Literal["day"]
    universe_membership_semantics: Literal["exact_snapshot"]
    selection_window: Literal["validation"]
    final_holdout_access: Literal[False]
    production_order_routing: Literal[False]

    @model_validator(mode="after")
    def lookback_contract(self) -> RealValidationConfig:
        if self.reversal_lookback_observations > self.momentum_lookback_observations:
            raise ValueError("reversal lookback cannot exceed momentum lookback")
        return self

    @classmethod
    def load(cls, path: Path) -> RealValidationConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchRealValidationError(
                "research_real_validation_config_invalid",
                "Real-validation configuration is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class AdjustedDailyBar:
    instrument: str
    timestamp: datetime
    available_at: datetime
    adjusted_close: Decimal
    volume: int
    source_manifest: str
    source_sha256: str
    interval: Literal["day"] = "day"
    price_adjustment: Literal["corporate_action_adjusted"] = "corporate_action_adjusted"

    def __post_init__(self) -> None:
        aware = all(
            value.tzinfo is not None and value.utcoffset() is not None
            for value in (self.timestamp, self.available_at)
        )
        valid_hash = len(self.source_sha256) == 64 and all(
            character in "0123456789abcdef" for character in self.source_sha256
        )
        if (
            not self.instrument.strip()
            or not aware
            or self.available_at < self.timestamp
            or self.adjusted_close <= 0
            or not self.adjusted_close.is_finite()
            or self.volume < 0
            or not self.source_manifest.strip()
            or not valid_hash
        ):
            raise ResearchRealValidationError(
                "research_real_validation_bar_invalid", "Adjusted daily bar is invalid"
            )


RunStatus = Literal["HOLDOUT_READY", "VALIDATION_REJECTED"]


@dataclass(frozen=True, slots=True)
class RealValidationResult:
    runner_id: str
    status: RunStatus
    feature_points: int
    universe_id: str
    universe_sha256: str
    source_artifacts: tuple[tuple[str, str], ...]
    evaluation: ModelEvaluationResult
    dossier: CandidateDossier | None
    final_holdout_access: Literal[False] = False
    final_holdout_consumed: Literal[False] = False
    eligible_for_operational_promotion: Literal[False] = False
    production_order_routing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class RealHistoricalValidationRunner:
    config: RealValidationConfig
    dataset_config: MLDatasetConfig
    evaluation_workflow: ModelEvaluationWorkflow
    freeze_gate: CandidateFreezeGate

    def __post_init__(self) -> None:
        expected_features = (
            "momentum_20d",
            "reversal_5d",
            "volatility_20d",
            "dollar_volume_rank",
        )
        if self.dataset_config.feature_names != expected_features:
            raise ResearchRealValidationError(
                "research_real_validation_feature_contract",
                "QR-14 requires the fixed QR-08 feature schema",
            )
        if (
            self.dataset_config.dataset_id != self.evaluation_workflow.config.required_dataset_id
            or self.freeze_gate.config.required_workflow_id
            != self.evaluation_workflow.config.workflow_id
            or self.freeze_gate.ridge_config.model_id
            != self.evaluation_workflow.ridge_config.model_id
            or self.freeze_gate.boosted_config.model_id
            != self.evaluation_workflow.boosted_config.model_id
        ):
            raise ResearchRealValidationError(
                "research_real_validation_identity_mismatch",
                "Dataset and evaluation identities do not match",
            )

    def run(
        self,
        bars: tuple[AdjustedDailyBar, ...],
        universe: PointInTimeUniverse,
        provenance: CandidateProvenance,
    ) -> RealValidationResult:
        points = assemble_real_feature_points(bars, universe, self.config)
        dataset = PurgedWalkForwardDatasetBuilder(self.dataset_config).build(points)
        evaluation = self.evaluation_workflow.evaluate(dataset)
        dossier = None
        status: RunStatus = "VALIDATION_REJECTED"
        if evaluation.stability.decision == "BOOSTED_VALIDATION_CANDIDATE":
            dossier = self.freeze_gate.freeze(evaluation, provenance)
            status = "HOLDOUT_READY"
        return RealValidationResult(
            self.config.runner_id,
            status,
            len(points),
            universe.manifest.universe_id,
            universe.manifest.data_sha256,
            tuple(sorted({(bar.source_manifest, bar.source_sha256) for bar in bars})),
            evaluation,
            dossier,
        )


def assemble_real_feature_points(
    bars: tuple[AdjustedDailyBar, ...],
    universe: PointInTimeUniverse,
    config: RealValidationConfig,
) -> tuple[MLFeaturePoint, ...]:
    if not bars:
        raise ResearchRealValidationError(
            "research_real_validation_empty", "No adjusted daily bars were supplied"
        )
    if universe.manifest.membership_semantics != config.universe_membership_semantics:
        raise ResearchRealValidationError(
            "research_real_validation_universe_invalid",
            "Universe must use exact-snapshot membership",
        )
    keys = tuple((bar.timestamp, bar.instrument) for bar in bars)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ResearchRealValidationError(
            "research_real_validation_order_invalid",
            "Bars must be unique and ordered by timestamp and instrument",
        )
    by_instrument: dict[str, list[AdjustedDailyBar]] = {}
    by_timestamp: dict[datetime, list[AdjustedDailyBar]] = {}
    for bar in bars:
        if (
            bar.interval != config.required_interval
            or bar.price_adjustment != config.required_price_adjustment
        ):
            raise ResearchRealValidationError(
                "research_real_validation_adjustment_required",
                "Only corporate-action-adjusted daily bars are accepted",
            )
        by_instrument.setdefault(bar.instrument, []).append(bar)
        by_timestamp.setdefault(bar.timestamp, []).append(bar)
    lookback = max(
        config.momentum_lookback_observations,
        config.volatility_lookback_observations,
    )
    history_index = {
        (bar.timestamp, instrument): index
        for instrument, series in by_instrument.items()
        for index, bar in enumerate(series)
    }
    points: list[MLFeaturePoint] = []
    for timestamp, dated_bars in sorted(by_timestamp.items()):
        availability_times = {bar.available_at for bar in dated_bars}
        if len(availability_times) != 1:
            raise ResearchRealValidationError(
                "research_real_validation_availability_mismatch",
                "A cross-section must share one point-in-time availability timestamp",
            )
        available_at = next(iter(availability_times))
        try:
            eligible = {member.instrument_key for member in universe.members_on(timestamp.date())}
        except ResearchUniverseError as error:
            raise ResearchRealValidationError(
                "research_real_validation_universe_gap",
                "Every feature date requires an exact universe observation",
            ) from error
        ready = [
            bar
            for bar in dated_bars
            if bar.instrument in eligible and history_index[(timestamp, bar.instrument)] >= lookback
        ]
        if not ready:
            continue
        if len(ready) < config.minimum_instruments_per_observation:
            raise ResearchRealValidationError(
                "research_real_validation_breadth_insufficient",
                "A usable observation has insufficient cross-sectional breadth",
            )
        dollar_volumes = {bar.instrument: bar.adjusted_close * bar.volume for bar in ready}
        for bar in sorted(ready, key=lambda item: item.instrument):
            series = by_instrument[bar.instrument]
            index = history_index[(timestamp, bar.instrument)]
            momentum = bar.adjusted_close / series[
                index - config.momentum_lookback_observations
            ].adjusted_close - Decimal(1)
            reversal = -(
                bar.adjusted_close
                / series[index - config.reversal_lookback_observations].adjusted_close
                - Decimal(1)
            )
            returns = tuple(
                series[position].adjusted_close / series[position - 1].adjusted_close - Decimal(1)
                for position in range(
                    index - config.volatility_lookback_observations + 1, index + 1
                )
            )
            volatility = _population_std(returns)
            rank = _percentile_rank(dollar_volumes, bar.instrument)
            points.append(
                MLFeaturePoint(
                    available_at,
                    bar.instrument,
                    {
                        "momentum_20d": momentum,
                        "reversal_5d": reversal,
                        "volatility_20d": volatility,
                        "dollar_volume_rank": rank,
                    },
                    bar.adjusted_close,
                    True,
                )
            )
    if not points:
        raise ResearchRealValidationError(
            "research_real_validation_history_insufficient",
            "No feature date has the required history",
        )
    return tuple(points)


def _population_std(values: tuple[Decimal, ...]) -> Decimal:
    mean = sum(values, Decimal(0)) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / len(values)
    with localcontext() as context:
        context.prec = 28
        return context.sqrt(variance)


def _percentile_rank(values: Mapping[str, Decimal], instrument: str) -> Decimal:
    value = values[instrument]
    lower = sum(1 for candidate in values.values() if candidate < value)
    equal = sum(1 for candidate in values.values() if candidate == value)
    if len(values) == 1:
        return Decimal(1)
    return (Decimal(lower) + Decimal(equal - 1) / Decimal(2)) / Decimal(len(values) - 1)
