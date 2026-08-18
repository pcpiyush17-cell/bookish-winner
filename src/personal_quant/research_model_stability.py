"""Validation-only stability gate for linear and nonlinear ML baselines."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_boosted_stumps import BoostedStumpsResult
from personal_quant.research_ridge_model import RidgeModelResult


class ResearchModelStabilityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ModelStabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    gate_id: str = Field(min_length=1)
    required_dataset_id: str = Field(min_length=1)
    required_ridge_model_id: str = Field(min_length=1)
    required_boosted_model_id: str = Field(min_length=1)
    minimum_folds: int = Field(ge=2)
    minimum_rmse_improvement_fraction: Decimal = Field(ge=0)
    minimum_positive_ic_fold_fraction: Decimal = Field(gt=0, le=1)
    maximum_degraded_rmse_fold_fraction: Decimal = Field(ge=0, lt=1)
    minimum_mean_information_coefficient: Decimal = Field(ge=0)
    minimum_selected_return_delta_pct: Decimal = Field(ge=0)
    required_cost_cases: tuple[str, ...]
    selection_window: Literal["validation"]
    holdout_access: Literal[False]
    production_order_routing: Literal[False]

    @field_validator(
        "minimum_rmse_improvement_fraction",
        "minimum_positive_ic_fold_fraction",
        "maximum_degraded_rmse_fold_fraction",
        "minimum_mean_information_coefficient",
        "minimum_selected_return_delta_pct",
        mode="before",
    )
    @classmethod
    def parse_decimals(cls, value: object) -> object:
        return Decimal(str(value)) if isinstance(value, (str, int, float)) else value

    @field_validator("required_cost_cases", mode="before")
    @classmethod
    def parse_cost_cases(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def complete_contract(self) -> ModelStabilityConfig:
        if self.required_cost_cases != ("1.0x", "1.5x", "2.0x"):
            raise ValueError("stability gate must require every cost case")
        return self

    @classmethod
    def load(cls, path: Path) -> ModelStabilityConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchModelStabilityError(
                "research_stability_config_invalid",
                "Research model-stability configuration is invalid",
            ) from error


GateDecision = Literal["RETAIN_RIDGE", "BOOSTED_VALIDATION_CANDIDATE"]


@dataclass(frozen=True, slots=True)
class FoldStabilityComparison:
    fold_number: int
    ridge_rmse: Decimal
    boosted_rmse: Decimal
    rmse_delta: Decimal
    ridge_information_coefficient: Decimal
    boosted_information_coefficient: Decimal
    identical_validation_samples: bool


@dataclass(frozen=True, slots=True)
class ModelStabilityMetrics:
    folds: int
    rmse_improvement_fraction: Decimal
    positive_ic_fold_fraction: Decimal
    degraded_rmse_fold_fraction: Decimal
    boosted_mean_information_coefficient: Decimal
    selected_return_delta_pct_by_cost: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_return_delta_pct_by_cost",
            MappingProxyType(dict(self.selected_return_delta_pct_by_cost)),
        )


@dataclass(frozen=True, slots=True)
class ModelStabilityResult:
    gate_id: str
    dataset_id: str
    dataset_sha256: str
    ridge_model_id: str
    boosted_model_id: str
    selection_window: Literal["validation"]
    decision: GateDecision
    failure_reasons: tuple[str, ...]
    fold_comparisons: tuple[FoldStabilityComparison, ...]
    metrics: ModelStabilityMetrics
    sha256: str
    holdout_access: Literal[False] = False
    eligible_for_operational_promotion: Literal[False] = False
    production_order_routing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ModelStabilityGate:
    config: ModelStabilityConfig

    def evaluate(
        self, ridge: RidgeModelResult, boosted: BoostedStumpsResult
    ) -> ModelStabilityResult:
        _validate_inputs(ridge, boosted, self.config)
        comparisons = tuple(
            FoldStabilityComparison(
                ridge_fold.fold_number,
                ridge_fold.validation_rmse,
                boosted_fold.validation_rmse,
                boosted_fold.validation_rmse - ridge_fold.validation_rmse,
                ridge_fold.information_coefficient,
                boosted_fold.information_coefficient,
                tuple(prediction.sample_id for prediction in ridge_fold.predictions)
                == tuple(prediction.sample_id for prediction in boosted_fold.predictions),
            )
            for ridge_fold, boosted_fold in zip(
                ridge.fold_results, boosted.fold_results, strict=True
            )
        )
        ridge_rmse = ridge.metrics.mean_validation_rmse
        boosted_rmse = boosted.metrics.mean_validation_rmse
        improvement = (ridge_rmse - boosted_rmse) / ridge_rmse if ridge_rmse > 0 else Decimal(0)
        positive_fraction = sum(
            (Decimal(1) for fold in boosted.fold_results if fold.information_coefficient > 0),
            Decimal(0),
        ) / len(boosted.fold_results)
        degraded_fraction = sum(
            (Decimal(1) for comparison in comparisons if comparison.rmse_delta > 0),
            Decimal(0),
        ) / len(comparisons)
        cost_deltas = {
            label: boosted.metrics.mean_selected_forward_return_pct_by_cost[label]
            - ridge.metrics.mean_selected_forward_return_pct_by_cost[label]
            for label in self.config.required_cost_cases
        }
        metrics = ModelStabilityMetrics(
            len(comparisons),
            improvement,
            positive_fraction,
            degraded_fraction,
            boosted.metrics.mean_information_coefficient,
            cost_deltas,
        )
        failures = _failure_reasons(comparisons, metrics, self.config)
        decision: GateDecision = "BOOSTED_VALIDATION_CANDIDATE" if not failures else "RETAIN_RIDGE"
        fingerprint = _fingerprint(
            self.config, ridge, boosted, comparisons, metrics, decision, failures
        )
        return ModelStabilityResult(
            self.config.gate_id,
            ridge.dataset_id,
            ridge.dataset_sha256,
            ridge.model_id,
            boosted.model_id,
            "validation",
            decision,
            failures,
            comparisons,
            metrics,
            fingerprint,
        )


def _failure_reasons(
    comparisons: tuple[FoldStabilityComparison, ...],
    metrics: ModelStabilityMetrics,
    config: ModelStabilityConfig,
) -> tuple[str, ...]:
    failures: list[str] = []
    if metrics.rmse_improvement_fraction < config.minimum_rmse_improvement_fraction:
        failures.append("rmse_improvement_insufficient")
    if metrics.positive_ic_fold_fraction < config.minimum_positive_ic_fold_fraction:
        failures.append("positive_ic_fold_fraction_insufficient")
    if metrics.degraded_rmse_fold_fraction > config.maximum_degraded_rmse_fold_fraction:
        failures.append("degraded_rmse_fold_fraction_excessive")
    if metrics.boosted_mean_information_coefficient < config.minimum_mean_information_coefficient:
        failures.append("mean_information_coefficient_insufficient")
    if any(
        value < config.minimum_selected_return_delta_pct
        for value in metrics.selected_return_delta_pct_by_cost.values()
    ):
        failures.append("cost_case_improvement_insufficient")
    if not all(comparison.identical_validation_samples for comparison in comparisons):
        failures.append("validation_samples_mismatch")
    return tuple(failures)


def _validate_inputs(
    ridge: RidgeModelResult,
    boosted: BoostedStumpsResult,
    config: ModelStabilityConfig,
) -> None:
    if (
        ridge.dataset_id != config.required_dataset_id
        or boosted.dataset_id != config.required_dataset_id
        or ridge.dataset_sha256 != boosted.dataset_sha256
    ):
        raise ResearchModelStabilityError(
            "research_stability_dataset_mismatch", "Model datasets do not match the gate"
        )
    if (
        ridge.model_id != config.required_ridge_model_id
        or boosted.model_id != config.required_boosted_model_id
    ):
        raise ResearchModelStabilityError(
            "research_stability_model_mismatch", "Model identities do not match the gate"
        )
    if (
        ridge.selection_window != "validation"
        or boosted.selection_window != "validation"
        or ridge.production_order_routing
        or boosted.production_order_routing
    ):
        raise ResearchModelStabilityError(
            "research_stability_input_unsafe", "Stability gate accepts validation-only results"
        )
    if (
        len(ridge.fold_results) != len(boosted.fold_results)
        or len(ridge.fold_results) < config.minimum_folds
    ):
        raise ResearchModelStabilityError(
            "research_stability_folds_invalid", "Model fold counts are incompatible"
        )
    if tuple(fold.fold_number for fold in ridge.fold_results) != tuple(
        fold.fold_number for fold in boosted.fold_results
    ):
        raise ResearchModelStabilityError(
            "research_stability_folds_invalid", "Model fold identities are incompatible"
        )
    required = set(config.required_cost_cases)
    if (
        set(ridge.metrics.mean_selected_forward_return_pct_by_cost) != required
        or set(boosted.metrics.mean_selected_forward_return_pct_by_cost) != required
    ):
        raise ResearchModelStabilityError(
            "research_stability_cost_cases_invalid", "Model cost cases are incompatible"
        )


def _fingerprint(
    config: ModelStabilityConfig,
    ridge: RidgeModelResult,
    boosted: BoostedStumpsResult,
    comparisons: tuple[FoldStabilityComparison, ...],
    metrics: ModelStabilityMetrics,
    decision: GateDecision,
    failures: tuple[str, ...],
) -> str:
    payload = {
        "config": config.model_dump(mode="json"),
        "dataset_sha256": ridge.dataset_sha256,
        "ridge_model_id": ridge.model_id,
        "boosted_model_id": boosted.model_id,
        "comparisons": [
            {
                "fold": item.fold_number,
                "ridge_rmse": str(item.ridge_rmse),
                "boosted_rmse": str(item.boosted_rmse),
                "rmse_delta": str(item.rmse_delta),
                "ridge_ic": str(item.ridge_information_coefficient),
                "boosted_ic": str(item.boosted_information_coefficient),
                "identical_validation_samples": item.identical_validation_samples,
            }
            for item in comparisons
        ],
        "metrics": {
            "folds": metrics.folds,
            "rmse_improvement_fraction": str(metrics.rmse_improvement_fraction),
            "positive_ic_fold_fraction": str(metrics.positive_ic_fold_fraction),
            "degraded_rmse_fold_fraction": str(metrics.degraded_rmse_fold_fraction),
            "boosted_mean_ic": str(metrics.boosted_mean_information_coefficient),
            "cost_deltas": {
                key: str(value)
                for key, value in sorted(metrics.selected_return_delta_pct_by_cost.items())
            },
        },
        "decision": decision,
        "failures": failures,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
