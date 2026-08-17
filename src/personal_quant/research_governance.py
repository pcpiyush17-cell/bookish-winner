"""Fail-closed governance contracts for the isolated quantitative research lab."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchGovernanceError(ValueError):
    """Research configuration failure with a stable operator-facing code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ResearchGovernance(BaseModel):
    """Versioned boundaries that keep research outside operational state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    governance_id: str = Field(min_length=1)
    workspace_root: Path
    state_root: Path
    prohibited_paths: tuple[Path, ...] = Field(min_length=1)
    required_cost_multipliers: tuple[Decimal, ...]
    require_clean_git: bool
    require_point_in_time_data: bool
    require_untouched_holdout: bool
    production_order_routing: Literal[False]
    wp14_evidence_mutation: Literal[False]

    @field_validator("workspace_root", "state_root", mode="before")
    @classmethod
    def parse_root_path(cls, value: object) -> object:
        return Path(str(value)) if isinstance(value, str) else value

    @field_validator("prohibited_paths", mode="before")
    @classmethod
    def parse_prohibited_paths(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(Path(str(item)) for item in value)
        return value

    @field_validator("required_cost_multipliers", mode="before")
    @classmethod
    def parse_cost_multipliers(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(Decimal(str(item)) for item in value)
        return value

    @field_validator("workspace_root", "state_root", "prohibited_paths")
    @classmethod
    def relative_paths_only(cls, value: Path | tuple[Path, ...]) -> Path | tuple[Path, ...]:
        paths = value if isinstance(value, tuple) else (value,)
        if any(path.is_absolute() or ".." in path.parts for path in paths):
            raise ValueError(
                "research governance paths must be project-relative and non-traversing"
            )
        return value

    @model_validator(mode="after")
    def required_safety_cases(self) -> ResearchGovernance:
        mandatory_protected = {
            Path("state/trading.sqlite"),
            Path("state/replay"),
            Path("state/session"),
            Path("backups/wp14"),
            Path("data/recordings"),
            Path("data/manifests"),
        }
        if self.workspace_root != Path("research") or self.state_root != Path("state/research"):
            raise ValueError("QR-00 research roots are fixed by the isolation contract")
        if not mandatory_protected.issubset(set(self.prohibited_paths)):
            raise ValueError("research governance omits a mandatory protected path")
        if self.workspace_root == self.state_root:
            raise ValueError("research workspace and state roots must differ")
        if self.required_cost_multipliers != (
            Decimal("1.0"),
            Decimal("1.5"),
            Decimal("2.0"),
        ):
            raise ValueError("research must report 1.0x, 1.5x, and 2.0x cost cases")
        return self

    @classmethod
    def load(cls, path: Path) -> ResearchGovernance:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchGovernanceError(
                "research_governance_invalid", "Research governance configuration is invalid"
            ) from error

    def validate_boundaries(self, project_root: Path) -> None:
        """Prove configured research roots do not overlap prohibited operational paths."""
        root = project_root.resolve()
        research_roots = tuple(
            (root / relative).resolve() for relative in (self.workspace_root, self.state_root)
        )
        prohibited = tuple((root / relative).resolve() for relative in self.prohibited_paths)
        if any(not _is_within(path, root) for path in (*research_roots, *prohibited)):
            raise ResearchGovernanceError(
                "research_path_escape", "A research governance path escapes the project root"
            )
        if any(
            _paths_overlap(research_root, operational_path)
            for research_root in research_roots
            for operational_path in prohibited
        ):
            raise ResearchGovernanceError(
                "research_operational_overlap",
                "Research roots overlap protected operational or WP-14 evidence paths",
            )


class DataSnapshot(BaseModel):
    """An immutable point-in-time input referenced by one experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime

    @field_validator("manifest_path", mode="before")
    @classmethod
    def parse_manifest_path(cls, value: object) -> object:
        return Path(str(value)) if isinstance(value, str) else value

    @model_validator(mode="after")
    def aware_and_relative(self) -> DataSnapshot:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("research data as_of must be timezone-aware")
        if self.manifest_path.is_absolute() or ".." in self.manifest_path.parts:
            raise ValueError("research data manifests must be project-relative")
        return self


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered_and_aware(self) -> TimeWindow:
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in (self.start, self.end)
        ):
            raise ValueError("research windows must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("research window start must precede end")
        return self


class ExperimentManifest(BaseModel):
    """Immutable experimental intent; results live in a separately hashed artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    experiment_id: str = Field(pattern=r"^QR-[0-9]{4}-[a-z0-9-]+$")
    created_at: datetime
    hypothesis: str = Field(min_length=20)
    strategy_family: Literal[
        "benchmark",
        "cross_sectional_momentum",
        "mean_reversion",
        "relative_value",
        "regime",
        "factor",
        "event",
        "machine_learning",
        "deep_learning",
    ]
    universe_manifest: DataSnapshot
    datasets: tuple[DataSnapshot, ...] = Field(min_length=1)
    config_path: Path
    code_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    uv_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    train: TimeWindow
    validation: TimeWindow
    holdout: TimeWindow
    cost_multipliers: tuple[Decimal, ...]
    status: Literal["DRAFT", "EVALUATED", "REJECTED"]
    wp14_isolated: Literal[True]
    production_order_routing: Literal[False]

    @field_validator("datasets", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("config_path", mode="before")
    @classmethod
    def parse_config_path(cls, value: object) -> object:
        return Path(str(value)) if isinstance(value, str) else value

    @field_validator("cost_multipliers", mode="before")
    @classmethod
    def parse_cost_multipliers(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(Decimal(str(item)) for item in value)
        return value

    @model_validator(mode="after")
    def leakage_and_cost_guards(self) -> ExperimentManifest:
        if (
            not self.train.end <= self.validation.start
            or not self.validation.end <= self.holdout.start
        ):
            raise ValueError("train, validation, and holdout windows must be ordered and disjoint")
        if self.cost_multipliers != (Decimal("1.0"), Decimal("1.5"), Decimal("2.0")):
            raise ValueError("experiments must report all required cost multipliers")
        if self.config_path.is_absolute() or ".." in self.config_path.parts:
            raise ValueError("research configuration path must be project-relative")
        return self

    @classmethod
    def load(cls, path: Path) -> ExperimentManifest:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchGovernanceError(
                "research_manifest_invalid", "Research experiment manifest is invalid"
            ) from error


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_within(left, right) or _is_within(right, left)
