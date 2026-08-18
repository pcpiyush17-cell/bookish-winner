"""Deterministic gradient-boosted decision-stump research baseline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_ml_dataset import MLDatasetResult, MLSample
from personal_quant.research_ridge_model import RidgeModelResult


class ResearchBoostedStumpsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class BoostedStumpsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    model_id: str = Field(min_length=1)
    required_dataset_id: str = Field(min_length=1)
    required_ridge_model_id: str = Field(min_length=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    estimators: int = Field(ge=1, le=100)
    learning_rate: Decimal = Field(gt=0, le=1)
    minimum_leaf_samples: int = Field(ge=2)
    maximum_threshold_candidates: int = Field(ge=2, le=128)
    maximum_absolute_prediction: Decimal = Field(gt=0)
    minimum_train_samples: int = Field(ge=5)
    selection_fraction: Decimal = Field(gt=0, le=1)
    require_positive_prediction: Literal[True]
    one_way_cost_bps: Decimal = Field(ge=0)
    cost_multipliers: tuple[Decimal, ...]
    selection_window: Literal["validation"]
    fixed_hyperparameters: Literal[True]
    production_order_routing: Literal[False]

    @field_validator("feature_names", mode="before")
    @classmethod
    def parse_feature_names(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("feature_names")
    @classmethod
    def unique_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() for name in value) or len(value) != len(set(value)):
            raise ValueError("feature names must be non-empty and unique")
        return value

    @field_validator(
        "learning_rate",
        "maximum_absolute_prediction",
        "selection_fraction",
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
    def complete_contract(self) -> BoostedStumpsConfig:
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("boosted stumps must report every required cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> BoostedStumpsConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchBoostedStumpsError(
                "research_boosted_config_invalid",
                "Research boosted-stumps configuration is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class DecisionStump:
    feature_name: str
    threshold: Decimal
    left_update: Decimal
    right_update: Decimal


@dataclass(frozen=True, slots=True)
class BoostedPrediction:
    sample_id: str
    predicted_return: Decimal
    actual_return: Decimal


@dataclass(frozen=True, slots=True)
class BoostedFoldResult:
    fold_number: int
    base_prediction: Decimal
    stumps: tuple[DecisionStump, ...]
    predictions: tuple[BoostedPrediction, ...]
    validation_rmse: Decimal
    baseline_rmse: Decimal
    information_coefficient: Decimal
    directional_accuracy: Decimal


@dataclass(frozen=True, slots=True)
class BoostedStumpsMetrics:
    folds: int
    positive_information_coefficient_folds: int
    mean_estimators_fitted: Decimal
    mean_validation_rmse: Decimal
    mean_baseline_rmse: Decimal
    mean_information_coefficient: Decimal
    mean_selected_forward_return_pct_by_cost: Mapping[str, Decimal]
    mean_equal_weight_forward_return_pct_by_cost: Mapping[str, Decimal]
    excess_return_pct_vs_equal_weight_by_cost: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        for name in (
            "mean_selected_forward_return_pct_by_cost",
            "mean_equal_weight_forward_return_pct_by_cost",
            "excess_return_pct_vs_equal_weight_by_cost",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class BoostedRidgeComparison:
    dataset_sha256: str
    rmse_delta: Decimal
    information_coefficient_delta: Decimal
    selected_return_delta_pct_by_cost: Mapping[str, Decimal]
    beats_ridge_all_cost_cases: bool
    eligible_for_operational_promotion: Literal[False] = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_return_delta_pct_by_cost",
            MappingProxyType(dict(self.selected_return_delta_pct_by_cost)),
        )


@dataclass(frozen=True, slots=True)
class BoostedStumpsResult:
    model_id: str
    dataset_id: str
    dataset_sha256: str
    selection_window: Literal["validation"]
    fold_results: tuple[BoostedFoldResult, ...]
    metrics: BoostedStumpsMetrics
    required_ridge_model_id: str
    eligible_for_operational_promotion: Literal[False] = False
    production_order_routing: Literal[False] = False

    def compare_to_ridge(self, ridge: RidgeModelResult) -> BoostedRidgeComparison:
        if (
            ridge.model_id != self.required_ridge_model_id
            or ridge.dataset_sha256 != self.dataset_sha256
            or ridge.dataset_id != self.dataset_id
        ):
            raise ResearchBoostedStumpsError(
                "research_boosted_ridge_mismatch",
                "Ridge result does not match the boosted-stumps evaluation",
            )
        cases = self.metrics.mean_selected_forward_return_pct_by_cost
        if set(cases) != set(ridge.metrics.mean_selected_forward_return_pct_by_cost):
            raise ResearchBoostedStumpsError(
                "research_boosted_cost_cases_mismatch", "Model cost cases do not match"
            )
        deltas = {
            label: value - ridge.metrics.mean_selected_forward_return_pct_by_cost[label]
            for label, value in cases.items()
        }
        return BoostedRidgeComparison(
            self.dataset_sha256,
            self.metrics.mean_validation_rmse - ridge.metrics.mean_validation_rmse,
            self.metrics.mean_information_coefficient - ridge.metrics.mean_information_coefficient,
            deltas,
            all(value > 0 for value in deltas.values()),
        )


@dataclass(frozen=True, slots=True)
class PurgedWalkForwardBoostedStumps:
    config: BoostedStumpsConfig

    def evaluate(self, dataset: MLDatasetResult) -> BoostedStumpsResult:
        _validate_dataset(dataset, self.config)
        samples = {sample.sample_id: sample for sample in dataset.samples}
        fold_results: list[BoostedFoldResult] = []
        selected_events: list[tuple[Decimal, bool]] = []
        equal_events: list[Decimal] = []
        for fold in dataset.folds:
            train = tuple(samples[sample_id] for sample_id in fold.train_sample_ids)
            validation = tuple(samples[sample_id] for sample_id in fold.validation_sample_ids)
            if len(train) < self.config.minimum_train_samples:
                raise ResearchBoostedStumpsError(
                    "research_boosted_train_short", "A fold has too few training samples"
                )
            base, stumps = _fit(train, self.config)
            predictions = tuple(
                BoostedPrediction(
                    sample.sample_id,
                    _predict(sample, base, stumps, self.config),
                    sample.forward_return,
                )
                for sample in validation
            )
            predicted = [prediction.predicted_return for prediction in predictions]
            actual = [prediction.actual_return for prediction in predictions]
            baseline = [base for _ in predictions]
            fold_results.append(
                BoostedFoldResult(
                    fold.fold_number,
                    base,
                    stumps,
                    predictions,
                    _rmse(predicted, actual),
                    _rmse(baseline, actual),
                    _correlation(predicted, actual),
                    sum(
                        (
                            Decimal(1)
                            for prediction in predictions
                            if (prediction.predicted_return >= 0) == (prediction.actual_return >= 0)
                        ),
                        Decimal(0),
                    )
                    / len(predictions),
                )
            )
            selected, equal = _economic_events(validation, predictions, self.config)
            selected_events.extend(selected)
            equal_events.extend(equal)
        metrics = _aggregate(tuple(fold_results), selected_events, equal_events, self.config)
        return BoostedStumpsResult(
            self.config.model_id,
            dataset.dataset_id,
            dataset.sha256,
            "validation",
            tuple(fold_results),
            metrics,
            self.config.required_ridge_model_id,
        )


def _fit(
    samples: tuple[MLSample, ...], config: BoostedStumpsConfig
) -> tuple[Decimal, tuple[DecisionStump, ...]]:
    base = sum((sample.forward_return for sample in samples), Decimal(0)) / len(samples)
    fitted = [base for _ in samples]
    stumps: list[DecisionStump] = []
    for _ in range(config.estimators):
        residuals = [
            sample.forward_return - prediction
            for sample, prediction in zip(samples, fitted, strict=True)
        ]
        candidate = _best_stump(samples, residuals, config)
        if candidate is None:
            break
        stumps.append(candidate)
        fitted = [
            prediction + _stump_update(sample, candidate)
            for sample, prediction in zip(samples, fitted, strict=True)
        ]
    return base, tuple(stumps)


def _best_stump(
    samples: tuple[MLSample, ...],
    residuals: list[Decimal],
    config: BoostedStumpsConfig,
) -> DecisionStump | None:
    best: tuple[Decimal, int, Decimal, DecisionStump] | None = None
    for feature_index, feature_name in enumerate(config.feature_names):
        values = [sample.features[feature_name] for sample in samples]
        for threshold in _thresholds(values, config.maximum_threshold_candidates):
            left = [index for index, value in enumerate(values) if value <= threshold]
            right = [index for index, value in enumerate(values) if value > threshold]
            if len(left) < config.minimum_leaf_samples or len(right) < config.minimum_leaf_samples:
                continue
            left_mean = sum((residuals[index] for index in left), Decimal(0)) / len(left)
            right_mean = sum((residuals[index] for index in right), Decimal(0)) / len(right)
            left_indexes = set(left)
            loss = sum(
                (
                    (residuals[index] - (left_mean if index in left_indexes else right_mean)) ** 2
                    for index in range(len(samples))
                ),
                Decimal(0),
            )
            stump = DecisionStump(
                feature_name,
                threshold,
                left_mean * config.learning_rate,
                right_mean * config.learning_rate,
            )
            ranked = (loss, feature_index, threshold, stump)
            if best is None or ranked[:3] < best[:3]:
                best = ranked
    return best[3] if best is not None else None


def _thresholds(values: list[Decimal], maximum: int) -> tuple[Decimal, ...]:
    unique = sorted(set(values))
    candidates = tuple((left + right) / Decimal(2) for left, right in pairwise(unique))
    if len(candidates) <= maximum:
        return candidates
    indexes = {index * (len(candidates) - 1) // (maximum - 1) for index in range(maximum)}
    return tuple(candidates[index] for index in sorted(indexes))


def _stump_update(sample: MLSample, stump: DecisionStump) -> Decimal:
    return (
        stump.left_update
        if sample.features[stump.feature_name] <= stump.threshold
        else stump.right_update
    )


def _predict(
    sample: MLSample,
    base: Decimal,
    stumps: tuple[DecisionStump, ...],
    config: BoostedStumpsConfig,
) -> Decimal:
    prediction = base + sum((_stump_update(sample, stump) for stump in stumps), Decimal(0))
    return min(
        config.maximum_absolute_prediction,
        max(-config.maximum_absolute_prediction, prediction),
    )


def _economic_events(
    validation: tuple[MLSample, ...],
    predictions: tuple[BoostedPrediction, ...],
    config: BoostedStumpsConfig,
) -> tuple[list[tuple[Decimal, bool]], list[Decimal]]:
    by_time: dict[datetime, list[tuple[MLSample, BoostedPrediction]]] = {}
    for sample, prediction in zip(validation, predictions, strict=True):
        by_time.setdefault(sample.signal_at, []).append((sample, prediction))
    selected_events: list[tuple[Decimal, bool]] = []
    equal_events: list[Decimal] = []
    for pairs in by_time.values():
        ranked = sorted(pairs, key=lambda pair: (-pair[1].predicted_return, pair[0].sample_id))
        count = max(
            1,
            int(
                (Decimal(len(ranked)) * config.selection_fraction).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        selected = ranked[:count]
        if config.require_positive_prediction:
            selected = [pair for pair in selected if pair[1].predicted_return > 0]
        selected_events.append(
            (
                sum((pair[0].forward_return for pair in selected), Decimal(0)) / len(selected)
                if selected
                else Decimal(0),
                bool(selected),
            )
        )
        equal_events.append(
            sum((pair[0].forward_return for pair in pairs), Decimal(0)) / len(pairs)
        )
    return selected_events, equal_events


def _aggregate(
    folds: tuple[BoostedFoldResult, ...],
    selected_events: list[tuple[Decimal, bool]],
    equal_events: list[Decimal],
    config: BoostedStumpsConfig,
) -> BoostedStumpsMetrics:
    selected_by_cost: dict[str, Decimal] = {}
    equal_by_cost: dict[str, Decimal] = {}
    excess_by_cost: dict[str, Decimal] = {}
    for multiplier in config.cost_multipliers:
        label = f"{multiplier}x"
        cost = config.one_way_cost_bps / Decimal(10000) * multiplier * Decimal(2)
        selected = (
            sum(
                (value - cost if traded else value for value, traded in selected_events),
                Decimal(0),
            )
            / len(selected_events)
            * Decimal(100)
        )
        equal = (
            sum((value - cost for value in equal_events), Decimal(0))
            / len(equal_events)
            * Decimal(100)
        )
        selected_by_cost[label] = selected
        equal_by_cost[label] = equal
        excess_by_cost[label] = selected - equal
    return BoostedStumpsMetrics(
        len(folds),
        sum(fold.information_coefficient > 0 for fold in folds),
        sum((Decimal(len(fold.stumps)) for fold in folds), Decimal(0)) / len(folds),
        sum((fold.validation_rmse for fold in folds), Decimal(0)) / len(folds),
        sum((fold.baseline_rmse for fold in folds), Decimal(0)) / len(folds),
        sum((fold.information_coefficient for fold in folds), Decimal(0)) / len(folds),
        selected_by_cost,
        equal_by_cost,
        excess_by_cost,
    )


def _rmse(predicted: list[Decimal], actual: list[Decimal]) -> Decimal:
    return _sqrt(
        sum(
            (
                (prediction - observation) ** 2
                for prediction, observation in zip(predicted, actual, strict=True)
            ),
            Decimal(0),
        )
        / len(actual)
    )


def _sample_std(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    mean = sum(values, Decimal(0)) / len(values)
    return _sqrt(sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1))


def _correlation(left: list[Decimal], right: list[Decimal]) -> Decimal:
    left_std = _sample_std(left)
    right_std = _sample_std(right)
    if left_std == 0 or right_std == 0:
        return Decimal(0)
    left_mean = sum(left, Decimal(0)) / len(left)
    right_mean = sum(right, Decimal(0)) / len(right)
    covariance = sum(
        (
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left, right, strict=True)
        ),
        Decimal(0),
    ) / (len(left) - 1)
    return covariance / (left_std * right_std)


def _sqrt(value: Decimal) -> Decimal:
    return value.sqrt()


def _validate_dataset(dataset: MLDatasetResult, config: BoostedStumpsConfig) -> None:
    if dataset.dataset_id != config.required_dataset_id:
        raise ResearchBoostedStumpsError(
            "research_boosted_dataset_mismatch", "Dataset identity does not match"
        )
    if dataset.selection_window != "validation" or dataset.production_order_routing:
        raise ResearchBoostedStumpsError(
            "research_boosted_dataset_unsafe", "Boosting accepts validation-only datasets"
        )
    expected = set(config.feature_names)
    if any(set(sample.features) != expected for sample in dataset.samples):
        raise ResearchBoostedStumpsError(
            "research_boosted_features_mismatch", "Dataset features do not match"
        )
    sample_ids = {sample.sample_id for sample in dataset.samples}
    if any(
        set(fold.train_sample_ids) - sample_ids or set(fold.validation_sample_ids) - sample_ids
        for fold in dataset.folds
    ):
        raise ResearchBoostedStumpsError(
            "research_boosted_fold_invalid", "A fold references an unknown sample"
        )
