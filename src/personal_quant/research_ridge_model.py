"""Interpretable ridge-regression baseline for purged walk-forward datasets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_ml_dataset import MLDatasetResult, MLSample


class ResearchRidgeModelError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RidgeModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    model_id: str = Field(min_length=1)
    required_dataset_id: str = Field(min_length=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    ridge_penalty: Decimal = Field(gt=0)
    feature_std_floor: Decimal = Field(gt=0)
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
        "ridge_penalty",
        "feature_std_floor",
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
    def complete_contract(self) -> RidgeModelConfig:
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("ridge baseline must report every required cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> RidgeModelConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchRidgeModelError(
                "research_ridge_config_invalid", "Research ridge configuration is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class RidgePrediction:
    sample_id: str
    predicted_return: Decimal
    actual_return: Decimal


@dataclass(frozen=True, slots=True)
class RidgeFoldResult:
    fold_number: int
    intercept: Decimal
    standardized_coefficients: Mapping[str, Decimal]
    feature_means: Mapping[str, Decimal]
    feature_stds: Mapping[str, Decimal]
    predictions: tuple[RidgePrediction, ...]
    validation_rmse: Decimal
    baseline_rmse: Decimal
    information_coefficient: Decimal
    directional_accuracy: Decimal

    def __post_init__(self) -> None:
        for name in ("standardized_coefficients", "feature_means", "feature_stds"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True, slots=True)
class RidgeModelMetrics:
    folds: int
    positive_information_coefficient_folds: int
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
class RidgeModelResult:
    model_id: str
    dataset_id: str
    dataset_sha256: str
    selection_window: Literal["validation"]
    fold_results: tuple[RidgeFoldResult, ...]
    metrics: RidgeModelMetrics
    eligible_for_operational_promotion: Literal[False] = False
    production_order_routing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class PurgedWalkForwardRidgeBaseline:
    config: RidgeModelConfig

    def evaluate(self, dataset: MLDatasetResult) -> RidgeModelResult:
        _validate_dataset(dataset, self.config)
        samples = {sample.sample_id: sample for sample in dataset.samples}
        fold_results: list[RidgeFoldResult] = []
        selected_events: list[tuple[Decimal, bool]] = []
        equal_weight_events: list[Decimal] = []
        for fold in dataset.folds:
            train = tuple(samples[sample_id] for sample_id in fold.train_sample_ids)
            validation = tuple(samples[sample_id] for sample_id in fold.validation_sample_ids)
            if len(train) < self.config.minimum_train_samples:
                raise ResearchRidgeModelError(
                    "research_ridge_train_short", "A fold has too few training samples"
                )
            fitted = _fit(train, self.config)
            predictions = tuple(
                RidgePrediction(
                    sample.sample_id,
                    _predict(sample, fitted, self.config),
                    sample.forward_return,
                )
                for sample in validation
            )
            actual = [prediction.actual_return for prediction in predictions]
            predicted = [prediction.predicted_return for prediction in predictions]
            baseline = [fitted.target_mean for _ in validation]
            fold_results.append(
                RidgeFoldResult(
                    fold.fold_number,
                    fitted.target_mean,
                    fitted.coefficients,
                    fitted.feature_means,
                    fitted.feature_stds,
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
            fold_selected, fold_equal = _economic_events(validation, predictions, self.config)
            selected_events.extend(fold_selected)
            equal_weight_events.extend(fold_equal)
        metrics = _aggregate_metrics(
            tuple(fold_results), selected_events, equal_weight_events, self.config
        )
        return RidgeModelResult(
            self.config.model_id,
            dataset.dataset_id,
            dataset.sha256,
            "validation",
            tuple(fold_results),
            metrics,
        )


@dataclass(frozen=True, slots=True)
class _FittedRidge:
    target_mean: Decimal
    coefficients: Mapping[str, Decimal]
    feature_means: Mapping[str, Decimal]
    feature_stds: Mapping[str, Decimal]


def _fit(samples: tuple[MLSample, ...], config: RidgeModelConfig) -> _FittedRidge:
    means = {
        name: sum((sample.features[name] for sample in samples), Decimal(0)) / len(samples)
        for name in config.feature_names
    }
    stds = {
        name: max(
            _sample_std([sample.features[name] for sample in samples]),
            config.feature_std_floor,
        )
        for name in config.feature_names
    }
    target_mean = sum((sample.forward_return for sample in samples), Decimal(0)) / len(samples)
    rows = [
        [(sample.features[name] - means[name]) / stds[name] for name in config.feature_names]
        for sample in samples
    ]
    centered_targets = [sample.forward_return - target_mean for sample in samples]
    dimension = len(config.feature_names)
    gram = [
        [
            sum((row[left] * row[right] for row in rows), Decimal(0))
            + (config.ridge_penalty if left == right else Decimal(0))
            for right in range(dimension)
        ]
        for left in range(dimension)
    ]
    rhs = [
        sum(
            (row[index] * target for row, target in zip(rows, centered_targets, strict=True)),
            Decimal(0),
        )
        for index in range(dimension)
    ]
    solved = _solve(gram, rhs)
    coefficients = dict(zip(config.feature_names, solved, strict=True))
    return _FittedRidge(target_mean, coefficients, means, stds)


def _predict(sample: MLSample, fitted: _FittedRidge, config: RidgeModelConfig) -> Decimal:
    prediction = fitted.target_mean + sum(
        (
            fitted.coefficients[name]
            * (sample.features[name] - fitted.feature_means[name])
            / fitted.feature_stds[name]
            for name in config.feature_names
        ),
        Decimal(0),
    )
    return min(
        config.maximum_absolute_prediction,
        max(-config.maximum_absolute_prediction, prediction),
    )


def _solve(matrix: list[list[Decimal]], rhs: list[Decimal]) -> list[Decimal]:
    size = len(rhs)
    augmented = [[*row, rhs[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if augmented[pivot][column] == 0:
            raise ResearchRidgeModelError(
                "research_ridge_singular", "Ridge system cannot be solved"
            )
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]


def _economic_events(
    validation: tuple[MLSample, ...],
    predictions: tuple[RidgePrediction, ...],
    config: RidgeModelConfig,
) -> tuple[list[tuple[Decimal, bool]], list[Decimal]]:
    by_time: dict[object, list[tuple[MLSample, RidgePrediction]]] = {}
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


def _aggregate_metrics(
    folds: tuple[RidgeFoldResult, ...],
    selected_events: list[tuple[Decimal, bool]],
    equal_events: list[Decimal],
    config: RidgeModelConfig,
) -> RidgeModelMetrics:
    selected_by_cost: dict[str, Decimal] = {}
    equal_by_cost: dict[str, Decimal] = {}
    excess_by_cost: dict[str, Decimal] = {}
    for multiplier in config.cost_multipliers:
        label = f"{multiplier}x"
        round_trip_cost = config.one_way_cost_bps / Decimal(10000) * multiplier * Decimal(2)
        selected = (
            sum(
                (value - round_trip_cost if traded else value for value, traded in selected_events),
                Decimal(0),
            )
            / len(selected_events)
            * Decimal(100)
        )
        equal = (
            sum((value - round_trip_cost for value in equal_events), Decimal(0))
            / len(equal_events)
            * Decimal(100)
        )
        selected_by_cost[label] = selected
        equal_by_cost[label] = equal
        excess_by_cost[label] = selected - equal
    return RidgeModelMetrics(
        len(folds),
        sum(fold.information_coefficient > 0 for fold in folds),
        sum((fold.validation_rmse for fold in folds), Decimal(0)) / len(folds),
        sum((fold.baseline_rmse for fold in folds), Decimal(0)) / len(folds),
        sum((fold.information_coefficient for fold in folds), Decimal(0)) / len(folds),
        selected_by_cost,
        equal_by_cost,
        excess_by_cost,
    )


def _rmse(predicted: list[Decimal], actual: list[Decimal]) -> Decimal:
    return _sqrt_decimal(
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
    return _sqrt_decimal(
        sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)
    )


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


def _sqrt_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.sqrt()


def _validate_dataset(dataset: MLDatasetResult, config: RidgeModelConfig) -> None:
    if dataset.dataset_id != config.required_dataset_id:
        raise ResearchRidgeModelError(
            "research_ridge_dataset_mismatch", "Dataset identity does not match the model contract"
        )
    if dataset.selection_window != "validation" or dataset.production_order_routing:
        raise ResearchRidgeModelError(
            "research_ridge_dataset_unsafe", "Ridge evaluation accepts validation-only datasets"
        )
    expected = set(config.feature_names)
    if any(set(sample.features) != expected for sample in dataset.samples):
        raise ResearchRidgeModelError(
            "research_ridge_features_mismatch", "Dataset features do not match the model contract"
        )
    sample_ids = {sample.sample_id for sample in dataset.samples}
    if any(
        set(fold.train_sample_ids) - sample_ids or set(fold.validation_sample_ids) - sample_ids
        for fold in dataset.folds
    ):
        raise ResearchRidgeModelError(
            "research_ridge_fold_invalid", "A model fold references an unknown sample"
        )
