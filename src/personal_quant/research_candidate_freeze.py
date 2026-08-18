"""Immutable candidate dossier and final-holdout readiness gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_boosted_stumps import BoostedStumpsConfig
from personal_quant.research_model_evaluation import ModelEvaluationResult
from personal_quant.research_ridge_model import RidgeModelConfig


class ResearchCandidateFreezeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CandidateFreezeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    freeze_id: str = Field(min_length=1)
    required_workflow_id: str = Field(min_length=1)
    required_stability_gate_id: str = Field(min_length=1)
    required_stability_decision: Literal["BOOSTED_VALIDATION_CANDIDATE"]
    required_config_artifacts: tuple[str, ...]
    selection_window: Literal["validation"]
    final_holdout_access: Literal[False]
    final_holdout_consumed: Literal[False]
    eligible_for_operational_promotion: Literal[False]
    production_order_routing: Literal[False]

    @field_validator("required_config_artifacts", mode="before")
    @classmethod
    def parse_artifacts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def complete_contract(self) -> CandidateFreezeConfig:
        expected = (
            "ml_dataset",
            "ridge",
            "boosted",
            "stability",
            "evaluation",
        )
        if self.required_config_artifacts != expected:
            raise ValueError("candidate freeze requires every ordered component configuration")
        return self

    @classmethod
    def load(cls, path: Path) -> CandidateFreezeConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchCandidateFreezeError(
                "research_candidate_freeze_config_invalid",
                "Research candidate-freeze configuration is invalid",
            ) from error


class FrozenArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    uv_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_artifacts: tuple[FrozenArtifact, ...]

    @field_validator("config_artifacts", mode="before")
    @classmethod
    def parse_artifacts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def unique_artifacts(self) -> CandidateProvenance:
        names = tuple(artifact.name for artifact in self.config_artifacts)
        paths = tuple(artifact.path for artifact in self.config_artifacts)
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            raise ValueError("candidate provenance artifacts must be unique")
        return self


@dataclass(frozen=True, slots=True)
class CandidateDossier:
    freeze_id: str
    status: Literal["HOLDOUT_READY"]
    workflow_id: str
    evaluation_report_sha256: str
    dataset_id: str
    dataset_sha256: str
    ridge_model_id: str
    boosted_model_id: str
    stability_gate_id: str
    stability_sha256: str
    git_commit_sha: str
    uv_lock_sha256: str
    config_artifacts: tuple[FrozenArtifact, ...]
    one_way_cost_bps: Decimal
    cost_multipliers: tuple[Decimal, ...]
    dossier_sha256: str
    selection_window: Literal["validation"] = "validation"
    final_holdout_access: Literal[False] = False
    final_holdout_consumed: Literal[False] = False
    eligible_for_operational_promotion: Literal[False] = False
    production_order_routing: Literal[False] = False

    def payload(self) -> dict[str, object]:
        return _dossier_payload(self, include_sha256=True)


@dataclass(frozen=True, slots=True)
class CandidateFreezeGate:
    config: CandidateFreezeConfig
    ridge_config: RidgeModelConfig
    boosted_config: BoostedStumpsConfig

    def __post_init__(self) -> None:
        if (
            self.ridge_config.model_id != self.boosted_config.required_ridge_model_id
            or self.ridge_config.required_dataset_id != self.boosted_config.required_dataset_id
        ):
            raise ResearchCandidateFreezeError(
                "research_candidate_freeze_model_mismatch",
                "Candidate model identities do not match",
            )
        if (
            self.ridge_config.feature_names != self.boosted_config.feature_names
            or self.ridge_config.one_way_cost_bps != self.boosted_config.one_way_cost_bps
            or self.ridge_config.cost_multipliers != self.boosted_config.cost_multipliers
        ):
            raise ResearchCandidateFreezeError(
                "research_candidate_freeze_contract_mismatch",
                "Candidate model feature or cost contracts do not match",
            )
        if (
            self.ridge_config.production_order_routing
            or self.boosted_config.production_order_routing
        ):
            raise ResearchCandidateFreezeError(
                "research_candidate_freeze_unsafe", "Candidate models must remain research-only"
            )

    def freeze(
        self, evaluation: ModelEvaluationResult, provenance: CandidateProvenance
    ) -> CandidateDossier:
        _validate_evaluation(evaluation, self.config, self.ridge_config, self.boosted_config)
        if tuple(artifact.name for artifact in provenance.config_artifacts) != (
            self.config.required_config_artifacts
        ):
            raise ResearchCandidateFreezeError(
                "research_candidate_freeze_provenance_incomplete",
                "Candidate provenance does not contain every required configuration",
            )
        provisional = CandidateDossier(
            self.config.freeze_id,
            "HOLDOUT_READY",
            evaluation.workflow_id,
            evaluation.report_sha256,
            evaluation.dataset_id,
            evaluation.dataset_sha256,
            evaluation.ridge.model_id,
            evaluation.boosted.model_id,
            evaluation.stability.gate_id,
            evaluation.stability.sha256,
            provenance.git_commit_sha,
            provenance.uv_lock_sha256,
            provenance.config_artifacts,
            self.ridge_config.one_way_cost_bps,
            self.ridge_config.cost_multipliers,
            "",
        )
        fingerprint = _fingerprint(_dossier_payload(provisional, include_sha256=False))
        return CandidateDossier(
            provisional.freeze_id,
            provisional.status,
            provisional.workflow_id,
            provisional.evaluation_report_sha256,
            provisional.dataset_id,
            provisional.dataset_sha256,
            provisional.ridge_model_id,
            provisional.boosted_model_id,
            provisional.stability_gate_id,
            provisional.stability_sha256,
            provisional.git_commit_sha,
            provisional.uv_lock_sha256,
            provisional.config_artifacts,
            provisional.one_way_cost_bps,
            provisional.cost_multipliers,
            fingerprint,
        )


def freeze_file(path: Path, name: str, project_root: Path) -> FrozenArtifact:
    try:
        resolved_root = project_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        relative = resolved_path.relative_to(resolved_root).as_posix()
        checksum = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    except (OSError, ValueError) as error:
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_artifact_invalid",
            "Candidate artifact must be a readable file inside the project",
        ) from error
    if not resolved_path.is_file():
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_artifact_invalid",
            "Candidate artifact must be a readable file inside the project",
        )
    return FrozenArtifact(name=name, path=relative, sha256=checksum)


def write_candidate_dossier(dossier: CandidateDossier, output_directory: Path) -> Path:
    path = output_directory / f"candidate-{dossier.dossier_sha256}.json"
    text = json.dumps(dossier.payload(), indent=2, sort_keys=True) + "\n"
    try:
        output_directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise ResearchCandidateFreezeError(
                    "research_candidate_freeze_dossier_conflict",
                    "Existing candidate dossier conflicts with its fingerprint",
                )
            return path
        path.write_text(text, encoding="utf-8", newline="\n")
    except ResearchCandidateFreezeError:
        raise
    except OSError as error:
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_dossier_failed",
            "Candidate dossier could not be stored",
        ) from error
    return path


def _validate_evaluation(
    evaluation: ModelEvaluationResult,
    config: CandidateFreezeConfig,
    ridge: RidgeModelConfig,
    boosted: BoostedStumpsConfig,
) -> None:
    if (
        evaluation.workflow_id != config.required_workflow_id
        or evaluation.ridge.model_id != ridge.model_id
        or evaluation.boosted.model_id != boosted.model_id
        or evaluation.stability.gate_id != config.required_stability_gate_id
    ):
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_evaluation_mismatch",
            "Evaluation identities do not match the freeze contract",
        )
    if (
        evaluation.stability.decision != config.required_stability_decision
        or evaluation.stability.failure_reasons
    ):
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_gate_failed",
            "Only a successful stability-gate candidate can be frozen",
        )
    if (
        evaluation.selection_window != "validation"
        or evaluation.final_holdout_access
        or evaluation.eligible_for_operational_promotion
        or evaluation.production_order_routing
    ):
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_unsafe",
            "Candidate evaluation must remain validation-only",
        )
    report = evaluation.report_payload()
    claimed = report.pop("report_sha256", None)
    if claimed != evaluation.report_sha256 or _fingerprint(report) != evaluation.report_sha256:
        raise ResearchCandidateFreezeError(
            "research_candidate_freeze_report_invalid",
            "Evaluation report fingerprint could not be verified",
        )


def _dossier_payload(dossier: CandidateDossier, *, include_sha256: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "freeze_id": dossier.freeze_id,
        "status": dossier.status,
        "workflow_id": dossier.workflow_id,
        "evaluation_report_sha256": dossier.evaluation_report_sha256,
        "dataset": {"id": dossier.dataset_id, "sha256": dossier.dataset_sha256},
        "models": {
            "ridge": dossier.ridge_model_id,
            "boosted": dossier.boosted_model_id,
        },
        "stability": {
            "gate_id": dossier.stability_gate_id,
            "sha256": dossier.stability_sha256,
        },
        "provenance": {
            "git_commit_sha": dossier.git_commit_sha,
            "uv_lock_sha256": dossier.uv_lock_sha256,
            "config_artifacts": [artifact.model_dump() for artifact in dossier.config_artifacts],
        },
        "cost_contract": {
            "one_way_cost_bps": str(dossier.one_way_cost_bps),
            "multipliers": [str(value) for value in dossier.cost_multipliers],
        },
        "selection_window": "validation",
        "final_holdout_access": False,
        "final_holdout_consumed": False,
        "eligible_for_operational_promotion": False,
        "production_order_routing": False,
    }
    if include_sha256:
        payload["dossier_sha256"] = dossier.dossier_sha256
    return payload


def _fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
