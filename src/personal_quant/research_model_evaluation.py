"""End-to-end validation-only model evaluation and audit reporting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from personal_quant.research_boosted_stumps import (
    BoostedStumpsConfig,
    BoostedStumpsMetrics,
    BoostedStumpsResult,
    PurgedWalkForwardBoostedStumps,
)
from personal_quant.research_ml_dataset import MLDatasetResult
from personal_quant.research_model_stability import (
    ModelStabilityConfig,
    ModelStabilityGate,
    ModelStabilityResult,
)
from personal_quant.research_ridge_model import (
    PurgedWalkForwardRidgeBaseline,
    RidgeModelConfig,
    RidgeModelMetrics,
    RidgeModelResult,
)


class ResearchModelEvaluationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ModelEvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    workflow_id: str = Field(min_length=1)
    required_dataset_id: str = Field(min_length=1)
    required_ridge_model_id: str = Field(min_length=1)
    required_boosted_model_id: str = Field(min_length=1)
    required_stability_gate_id: str = Field(min_length=1)
    selection_window: Literal["validation"]
    final_holdout_access: Literal[False]
    eligible_for_operational_promotion: Literal[False]
    production_order_routing: Literal[False]

    @classmethod
    def load(cls, path: Path) -> ModelEvaluationConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchModelEvaluationError(
                "research_model_evaluation_config_invalid",
                "Research model-evaluation configuration is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class ModelEvaluationResult:
    workflow_id: str
    dataset_id: str
    dataset_sha256: str
    ridge: RidgeModelResult
    boosted: BoostedStumpsResult
    stability: ModelStabilityResult
    report_sha256: str
    selection_window: Literal["validation"] = "validation"
    final_holdout_access: Literal[False] = False
    eligible_for_operational_promotion: Literal[False] = False
    production_order_routing: Literal[False] = False

    def report_payload(self) -> dict[str, object]:
        return _report_payload(self, include_report_sha256=True)


@dataclass(frozen=True, slots=True)
class ModelEvaluationWorkflow:
    config: ModelEvaluationConfig
    ridge_config: RidgeModelConfig
    boosted_config: BoostedStumpsConfig
    stability_config: ModelStabilityConfig

    def __post_init__(self) -> None:
        _validate_contracts(
            self.config,
            self.ridge_config,
            self.boosted_config,
            self.stability_config,
        )

    def evaluate(self, dataset: MLDatasetResult) -> ModelEvaluationResult:
        if (
            dataset.dataset_id != self.config.required_dataset_id
            or dataset.selection_window != "validation"
            or dataset.production_order_routing
        ):
            raise ResearchModelEvaluationError(
                "research_model_evaluation_dataset_invalid",
                "Workflow accepts only its required validation dataset",
            )
        ridge = PurgedWalkForwardRidgeBaseline(self.ridge_config).evaluate(dataset)
        boosted = PurgedWalkForwardBoostedStumps(self.boosted_config).evaluate(dataset)
        stability = ModelStabilityGate(self.stability_config).evaluate(ridge, boosted)
        provisional = ModelEvaluationResult(
            self.config.workflow_id,
            dataset.dataset_id,
            dataset.sha256,
            ridge,
            boosted,
            stability,
            "",
        )
        fingerprint = _fingerprint(_report_payload(provisional, include_report_sha256=False))
        return ModelEvaluationResult(
            self.config.workflow_id,
            dataset.dataset_id,
            dataset.sha256,
            ridge,
            boosted,
            stability,
            fingerprint,
        )


def write_model_evaluation_report(result: ModelEvaluationResult, output_directory: Path) -> Path:
    report_directory = output_directory / result.dataset_sha256
    report_path = report_directory / f"model-evaluation-{result.report_sha256}.json"
    payload = json.dumps(result.report_payload(), indent=2, sort_keys=True) + "\n"
    try:
        report_directory.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            if report_path.read_text(encoding="utf-8") != payload:
                raise ResearchModelEvaluationError(
                    "research_model_evaluation_report_conflict",
                    "Existing evaluation report does not match its fingerprint",
                )
            return report_path
        report_path.write_text(payload, encoding="utf-8", newline="\n")
    except ResearchModelEvaluationError:
        raise
    except OSError as error:
        raise ResearchModelEvaluationError(
            "research_model_evaluation_report_failed",
            "Model-evaluation report could not be stored",
        ) from error
    return report_path


def _validate_contracts(
    config: ModelEvaluationConfig,
    ridge: RidgeModelConfig,
    boosted: BoostedStumpsConfig,
    stability: ModelStabilityConfig,
) -> None:
    identities_match = (
        config.required_dataset_id
        == ridge.required_dataset_id
        == boosted.required_dataset_id
        == stability.required_dataset_id
        and config.required_ridge_model_id
        == ridge.model_id
        == boosted.required_ridge_model_id
        == stability.required_ridge_model_id
        and config.required_boosted_model_id
        == boosted.model_id
        == stability.required_boosted_model_id
        and config.required_stability_gate_id == stability.gate_id
    )
    features_match = ridge.feature_names == boosted.feature_names
    costs_match = ridge.cost_multipliers == boosted.cost_multipliers
    safe = (
        ridge.selection_window
        == boosted.selection_window
        == stability.selection_window
        == "validation"
        and not ridge.production_order_routing
        and not boosted.production_order_routing
        and not stability.holdout_access
        and not stability.production_order_routing
    )
    if not identities_match:
        raise ResearchModelEvaluationError(
            "research_model_evaluation_identity_mismatch",
            "Model-evaluation component identities do not match",
        )
    if not features_match or not costs_match:
        raise ResearchModelEvaluationError(
            "research_model_evaluation_contract_mismatch",
            "Model feature or cost contracts do not match",
        )
    if not safe:
        raise ResearchModelEvaluationError(
            "research_model_evaluation_unsafe",
            "Model-evaluation components must remain validation-only",
        )


def _report_payload(
    result: ModelEvaluationResult, *, include_report_sha256: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "workflow_id": result.workflow_id,
        "dataset": {"id": result.dataset_id, "sha256": result.dataset_sha256},
        "ridge": _model_summary(result.ridge.model_id, result.ridge.metrics),
        "boosted": _model_summary(result.boosted.model_id, result.boosted.metrics),
        "stability": {
            "gate_id": result.stability.gate_id,
            "sha256": result.stability.sha256,
            "decision": result.stability.decision,
            "failure_reasons": list(result.stability.failure_reasons),
            "rmse_improvement_fraction": str(result.stability.metrics.rmse_improvement_fraction),
            "positive_ic_fold_fraction": str(result.stability.metrics.positive_ic_fold_fraction),
            "degraded_rmse_fold_fraction": str(
                result.stability.metrics.degraded_rmse_fold_fraction
            ),
            "selected_return_delta_pct_by_cost": {
                key: str(value)
                for key, value in sorted(
                    result.stability.metrics.selected_return_delta_pct_by_cost.items()
                )
            },
        },
        "selection_window": "validation",
        "final_holdout_access": False,
        "eligible_for_operational_promotion": False,
        "production_order_routing": False,
    }
    if include_report_sha256:
        payload["report_sha256"] = result.report_sha256
    return payload


def _model_summary(
    model_id: str, metrics: RidgeModelMetrics | BoostedStumpsMetrics
) -> dict[str, object]:
    return {
        "model_id": model_id,
        "folds": metrics.folds,
        "mean_validation_rmse": str(metrics.mean_validation_rmse),
        "mean_information_coefficient": str(metrics.mean_information_coefficient),
        "mean_selected_forward_return_pct_by_cost": {
            key: str(value)
            for key, value in sorted(metrics.mean_selected_forward_return_pct_by_cost.items())
        },
    }


def _fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
