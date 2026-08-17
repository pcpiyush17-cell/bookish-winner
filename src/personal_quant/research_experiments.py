"""Immutable experiment registry and leakage-safe research evaluation contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_governance import ExperimentManifest, TimeWindow


class ResearchExperimentError(ValueError):
    """Experiment registry or evaluation failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReproducibilityContext:
    git_commit: str
    git_clean: bool
    uv_lock_path: Path


@dataclass(frozen=True, slots=True)
class RegisteredExperiment:
    experiment_id: str
    manifest_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold: int
    train: TimeWindow
    validation: TimeWindow
    embargo_until: datetime


def build_walk_forward_folds(
    *,
    train_start: datetime,
    validation_start: datetime,
    validation_end: datetime,
    folds: int,
    purge: timedelta,
    embargo: timedelta,
) -> tuple[WalkForwardFold, ...]:
    """Build anchored folds whose validation blocks are separated by explicit embargoes."""
    _aware(train_start, validation_start, validation_end)
    if train_start >= validation_start or validation_start >= validation_end:
        raise ResearchExperimentError(
            "research_walk_forward_window_invalid", "Walk-forward windows are not ordered"
        )
    if folds < 1 or purge < timedelta(0) or embargo < timedelta(0):
        raise ResearchExperimentError(
            "research_walk_forward_parameters_invalid", "Fold count and gaps must be non-negative"
        )
    usable = validation_end - validation_start - embargo * (folds - 1)
    if usable <= timedelta(0):
        raise ResearchExperimentError(
            "research_walk_forward_space_short", "Validation window is too short for embargoes"
        )
    width = usable / folds
    result: list[WalkForwardFold] = []
    start = validation_start
    for index in range(folds):
        end = validation_end if index == folds - 1 else start + width
        train_end = start - purge
        if train_end <= train_start:
            raise ResearchExperimentError(
                "research_walk_forward_train_short", "Purge leaves no training observations"
            )
        result.append(
            WalkForwardFold(
                fold=index + 1,
                train=TimeWindow(start=train_start, end=train_end),
                validation=TimeWindow(start=start, end=end),
                embargo_until=end + embargo,
            )
        )
        start = end + embargo
    return tuple(result)


def classify_timestamp(manifest: ExperimentManifest, timestamp: datetime) -> str | None:
    """Assign an observation to exactly one declared window using half-open intervals."""
    _aware(timestamp)
    for name, window in (
        ("train", manifest.train),
        ("validation", manifest.validation),
        ("holdout", manifest.holdout),
    ):
        if window.start <= timestamp < window.end:
            return name
    return None


class SplitMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    observations: int = Field(ge=1)
    trading_days: int = Field(ge=1)
    trades: int = Field(ge=0)
    net_return_pct_by_cost: dict[str, Decimal]
    maximum_drawdown_pct: Decimal = Field(ge=0)
    turnover: Decimal = Field(ge=0)
    sharpe: Decimal | None = None
    annualized_metrics_headlined: bool = False

    @field_validator("net_return_pct_by_cost", mode="before")
    @classmethod
    def parse_cost_returns(cls, value: object) -> object:
        if isinstance(value, dict):
            return {str(key): Decimal(str(item)) for key, item in value.items()}
        return value

    @model_validator(mode="after")
    def required_costs_and_short_sample_guard(self) -> SplitMetrics:
        if set(self.net_return_pct_by_cost) != {"1.0x", "1.5x", "2.0x"}:
            raise ValueError("split metrics must report 1.0x, 1.5x, and 2.0x costs")
        if self.trading_days < 252 and self.annualized_metrics_headlined:
            raise ValueError("annualized metrics cannot be headlined for short samples")
        return self


class ExperimentResult(BaseModel):
    """One immutable evaluation result; it can never approve operational promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    experiment_id: str = Field(pattern=r"^QR-[0-9]{4}-[a-z0-9-]+$")
    created_at: datetime
    experiment_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_code_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    selection_window: Literal["validation"]
    holdout_evaluations: Literal[1]
    train: SplitMetrics
    validation: SplitMetrics
    holdout: SplitMetrics
    decision: Literal["REJECTED", "CANDIDATE"]
    warnings: tuple[str, ...] = ()
    wp14_isolated: Literal[True]
    production_order_routing: Literal[False]

    @field_validator("warnings", mode="before")
    @classmethod
    def freeze_warnings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def aware_result_time(self) -> ExperimentResult:
        _aware(self.created_at)
        return self

    @classmethod
    def load(cls, path: Path) -> ExperimentResult:
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ResearchExperimentError(
                "research_result_invalid", "Research result is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class ExperimentRegistry:
    root: Path
    holdout_state_root: Path

    def register(
        self,
        manifest_path: Path,
        *,
        project_root: Path,
        context: ReproducibilityContext,
    ) -> RegisteredExperiment:
        _validate_roots(self.root, self.holdout_state_root, project_root)
        manifest = ExperimentManifest.load(manifest_path)
        if manifest.status != "DRAFT":
            raise ResearchExperimentError(
                "research_experiment_not_draft", "Only DRAFT experiments may be registered"
            )
        if not context.git_clean or context.git_commit != manifest.code_commit:
            raise ResearchExperimentError(
                "research_experiment_git_mismatch", "Clean Git commit does not match experiment"
            )
        _verify_file(context.uv_lock_path, manifest.uv_lock_sha256, project_root)
        _verify_file(project_root / manifest.config_path, manifest.config_sha256, project_root)
        _verify_file(
            project_root / manifest.universe_manifest.manifest_path,
            manifest.universe_manifest.sha256,
            project_root,
        )
        for dataset in manifest.datasets:
            _verify_file(project_root / dataset.manifest_path, dataset.sha256, project_root)
        canonical = (
            json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        checksum = hashlib.sha256(canonical.encode()).hexdigest()
        target = self.root / manifest.experiment_id
        stored = target / "experiment.json"
        if target.exists():
            if not stored.exists() or _checksum(stored) != checksum:
                raise ResearchExperimentError(
                    "research_experiment_conflict", "A different registered experiment exists"
                )
            return RegisteredExperiment(manifest.experiment_id, stored, checksum)
        target.mkdir(parents=True, exist_ok=False)
        _atomic_write(stored, canonical)
        return RegisteredExperiment(manifest.experiment_id, stored, checksum)

    def save_result(self, result: ExperimentResult, *, project_root: Path) -> Path:
        _validate_roots(self.root, self.holdout_state_root, project_root)
        directory = self.root / result.experiment_id
        experiment_path = directory / "experiment.json"
        if (
            not experiment_path.exists()
            or _checksum(experiment_path) != result.experiment_manifest_sha256
        ):
            raise ResearchExperimentError(
                "research_result_manifest_mismatch", "Result does not match registered experiment"
            )
        try:
            registered = json.loads(experiment_path.read_text(encoding="utf-8"))
            registered_id = str(registered["experiment_id"])
            registered_commit = str(registered["code_commit"])
            if registered.get("schema_version") != 1:
                raise ValueError("unsupported registered experiment schema")
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            raise ResearchExperimentError(
                "research_experiment_registered_invalid", "Registered experiment is invalid"
            ) from error
        if (
            registered_id != result.experiment_id
            or registered_commit != result.evaluated_code_commit
        ):
            raise ResearchExperimentError(
                "research_result_identity_mismatch",
                "Result identity or evaluated commit does not match the experiment",
            )
        result_path = directory / "result.json"
        if result_path.exists():
            raise ResearchExperimentError(
                "research_result_exists", "Research results are immutable and already exist"
            )
        claim = self.holdout_state_root / f"{result.experiment_id}.json"
        _claim_holdout(claim, result)
        text = (
            json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        )
        _atomic_write(result_path, text)
        _atomic_write(
            directory / "result-manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "experiment_id": result.experiment_id,
                    "result_path": result_path.resolve()
                    .relative_to(project_root.resolve())
                    .as_posix(),
                    "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        return result_path

    def verify_result(self, result_manifest_path: Path, *, project_root: Path) -> ExperimentResult:
        _validate_roots(self.root, self.holdout_state_root, project_root)
        try:
            raw = json.loads(result_manifest_path.read_text(encoding="utf-8"))
            result_path = (project_root / str(raw["result_path"])).resolve()
            if not _is_within(result_path, self.root.resolve()):
                raise ResearchExperimentError(
                    "research_result_path_escape", "Result path escapes registry root"
                )
            if _checksum(result_path) != raw["result_sha256"]:
                raise ResearchExperimentError(
                    "research_result_checksum", "Result checksum does not match"
                )
            result = ExperimentResult.load(result_path)
            if raw.get("schema_version") != 1 or raw.get("experiment_id") != result.experiment_id:
                raise ResearchExperimentError(
                    "research_result_manifest_identity",
                    "Result manifest identity does not match its result",
                )
            return result
        except ResearchExperimentError:
            raise
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            raise ResearchExperimentError(
                "research_result_manifest_invalid", "Result manifest is invalid"
            ) from error


def _claim_holdout(path: Path, result: ExperimentResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "experiment_id": result.experiment_id,
                    "claimed_at": result.created_at.astimezone(UTC).isoformat(),
                    "result_manifest_sha256": result.experiment_manifest_sha256,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
    except FileExistsError as error:
        raise ResearchExperimentError(
            "research_holdout_already_used", "Final holdout has already been evaluated"
        ) from error


def _validate_roots(registry: Path, holdout: Path, project: Path) -> None:
    root = project.resolve()
    if not _is_within(registry.resolve(), (root / "research/results").resolve()) or not _is_within(
        holdout.resolve(), (root / "state/research").resolve()
    ):
        raise ResearchExperimentError(
            "research_registry_path_prohibited", "Experiment roots violate QR-00 isolation"
        )


def _verify_file(path: Path, expected: str, project_root: Path) -> None:
    resolved = path.resolve()
    if not _is_within(resolved, project_root.resolve()) or _checksum(resolved) != expected:
        raise ResearchExperimentError(
            "research_experiment_input_mismatch", "Experiment input checksum does not match"
        )


def _checksum(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ResearchExperimentError(
            "research_experiment_input_unreadable", "Experiment input cannot be read"
        ) from error


def _atomic_write(path: Path, content: str) -> None:
    partial = path.with_suffix(path.suffix + ".partial")
    try:
        partial.write_text(content, encoding="utf-8", newline="\n")
        partial.replace(path)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise ResearchExperimentError(
            "research_experiment_write_failed", "Research artifact could not be written"
        ) from error


def _aware(*values: datetime) -> None:
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ResearchExperimentError(
            "research_experiment_time_naive", "Research timestamps must be timezone-aware"
        )


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
