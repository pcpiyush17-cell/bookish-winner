"""Leakage-safe supervised-learning dataset and purged walk-forward folds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchMLDatasetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MLDatasetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    dataset_id: str = Field(min_length=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    label_horizon_observations: int = Field(ge=1)
    minimum_train_observations: int = Field(ge=5)
    validation_observations: int = Field(ge=2)
    purge_observations: int = Field(ge=1)
    embargo_observations: int = Field(ge=0)
    minimum_folds: int = Field(ge=2)
    signal_execution_lag_observations: Literal[1]
    split_method: Literal["expanding_purged_walk_forward"]
    selection_window: Literal["validation"]
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

    @model_validator(mode="after")
    def complete_contract(self) -> MLDatasetConfig:
        required_purge = self.label_horizon_observations + self.signal_execution_lag_observations
        if self.purge_observations < required_purge:
            raise ValueError("purge must cover the execution lag and label horizon")
        return self

    @classmethod
    def load(cls, path: Path) -> MLDatasetConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchMLDatasetError(
                "research_ml_config_invalid", "Research ML dataset configuration is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class MLFeaturePoint:
    timestamp: datetime
    instrument: str
    features: Mapping[str, Decimal]
    price: Decimal
    eligible: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ResearchMLDatasetError(
                "research_ml_time_naive", "Feature timestamp must be timezone-aware"
            )
        if not self.instrument.strip():
            raise ResearchMLDatasetError(
                "research_ml_instrument_invalid", "Feature instrument is invalid"
            )
        if (
            self.price <= 0
            or not self.price.is_finite()
            or any(not value.is_finite() for value in self.features.values())
        ):
            raise ResearchMLDatasetError(
                "research_ml_value_invalid", "Feature values and prices must be finite"
            )


@dataclass(frozen=True, slots=True)
class MLSample:
    sample_id: str
    instrument: str
    signal_at: datetime
    label_start_at: datetime
    label_end_at: datetime
    features: Mapping[str, Decimal]
    forward_return: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))


@dataclass(frozen=True, slots=True)
class PurgedWalkForwardFold:
    fold_number: int
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]
    train_signal_start: datetime
    train_signal_end: datetime
    validation_signal_start: datetime
    validation_signal_end: datetime
    purged_observations: int
    embargoed_observations_after: int


@dataclass(frozen=True, slots=True)
class MLDatasetResult:
    dataset_id: str
    selection_window: Literal["validation"]
    samples: tuple[MLSample, ...]
    folds: tuple[PurgedWalkForwardFold, ...]
    sha256: str
    production_order_routing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class PurgedWalkForwardDatasetBuilder:
    config: MLDatasetConfig

    def build(self, points: tuple[MLFeaturePoint, ...]) -> MLDatasetResult:
        _validate_points(points, self.config)
        samples = _build_samples(points, self.config)
        folds = _build_folds(samples, self.config)
        fingerprint = _fingerprint(self.config, samples, folds)
        return MLDatasetResult(
            self.config.dataset_id,
            "validation",
            samples,
            folds,
            fingerprint,
        )


def _build_samples(
    points: tuple[MLFeaturePoint, ...], config: MLDatasetConfig
) -> tuple[MLSample, ...]:
    by_instrument: dict[str, list[MLFeaturePoint]] = {}
    for point in points:
        by_instrument.setdefault(point.instrument, []).append(point)
    samples: list[MLSample] = []
    endpoint_offset = config.signal_execution_lag_observations + config.label_horizon_observations
    for instrument, series in sorted(by_instrument.items()):
        for index in range(len(series) - endpoint_offset):
            signal = series[index]
            if not signal.eligible:
                continue
            label_start = series[index + config.signal_execution_lag_observations]
            label_end = series[index + endpoint_offset]
            label_path = series[
                index + config.signal_execution_lag_observations : index + endpoint_offset + 1
            ]
            if any(not point.eligible for point in label_path):
                continue
            forward_return = label_end.price / label_start.price - Decimal(1)
            sample_id = f"{instrument}|{signal.timestamp.isoformat()}"
            samples.append(
                MLSample(
                    sample_id,
                    instrument,
                    signal.timestamp,
                    label_start.timestamp,
                    label_end.timestamp,
                    signal.features,
                    forward_return,
                )
            )
    return tuple(sorted(samples, key=lambda sample: (sample.signal_at, sample.instrument)))


def _build_folds(
    samples: tuple[MLSample, ...], config: MLDatasetConfig
) -> tuple[PurgedWalkForwardFold, ...]:
    signal_times = tuple(sorted({sample.signal_at for sample in samples}))
    validation_start = config.minimum_train_observations + config.purge_observations
    folds: list[PurgedWalkForwardFold] = []
    while validation_start + config.validation_observations <= len(signal_times):
        train_end = validation_start - config.purge_observations
        train_times = signal_times[:train_end]
        validation_times = signal_times[
            validation_start : validation_start + config.validation_observations
        ]
        train_ids = tuple(
            sample.sample_id
            for sample in samples
            if sample.signal_at in train_times and sample.label_end_at < validation_times[0]
        )
        validation_ids = tuple(
            sample.sample_id for sample in samples if sample.signal_at in validation_times
        )
        if train_ids and validation_ids:
            folds.append(
                PurgedWalkForwardFold(
                    len(folds) + 1,
                    train_ids,
                    validation_ids,
                    train_times[0],
                    train_times[-1],
                    validation_times[0],
                    validation_times[-1],
                    config.purge_observations,
                    config.embargo_observations,
                )
            )
        validation_start += config.validation_observations + config.embargo_observations
    if len(folds) < config.minimum_folds:
        raise ResearchMLDatasetError(
            "research_ml_folds_insufficient",
            "Dataset cannot produce the required walk-forward folds",
        )
    return tuple(folds)


def _fingerprint(
    config: MLDatasetConfig,
    samples: tuple[MLSample, ...],
    folds: tuple[PurgedWalkForwardFold, ...],
) -> str:
    payload = {
        "config": config.model_dump(mode="json"),
        "samples": [
            {
                "sample_id": sample.sample_id,
                "instrument": sample.instrument,
                "signal_at": sample.signal_at.isoformat(),
                "label_start_at": sample.label_start_at.isoformat(),
                "label_end_at": sample.label_end_at.isoformat(),
                "features": {key: str(value) for key, value in sorted(sample.features.items())},
                "forward_return": str(sample.forward_return),
            }
            for sample in samples
        ],
        "folds": [
            {
                "fold_number": fold.fold_number,
                "train_sample_ids": fold.train_sample_ids,
                "validation_sample_ids": fold.validation_sample_ids,
                "train_signal_start": fold.train_signal_start.isoformat(),
                "train_signal_end": fold.train_signal_end.isoformat(),
                "validation_signal_start": fold.validation_signal_start.isoformat(),
                "validation_signal_end": fold.validation_signal_end.isoformat(),
                "purged_observations": fold.purged_observations,
                "embargoed_observations_after": fold.embargoed_observations_after,
            }
            for fold in folds
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_points(points: tuple[MLFeaturePoint, ...], config: MLDatasetConfig) -> None:
    if not points:
        raise ResearchMLDatasetError("research_ml_points_empty", "Feature panel is empty")
    keys = tuple((point.timestamp, point.instrument) for point in points)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ResearchMLDatasetError(
            "research_ml_order_invalid", "Feature points must be unique and chronologically ordered"
        )
    expected = set(config.feature_names)
    if any(set(point.features) != expected for point in points):
        raise ResearchMLDatasetError(
            "research_ml_features_invalid", "Feature schema does not match the versioned contract"
        )
