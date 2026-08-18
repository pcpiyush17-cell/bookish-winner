"""Licensed research-data package validation and deterministic import."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.research_real_validation import AdjustedDailyBar
from personal_quant.research_universe import (
    PointInTimeUniverse,
    UniverseManifest,
    UniverseMember,
    UniverseObservation,
)


class ResearchDataReadinessError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DataReadinessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    policy_id: str = Field(min_length=1)
    minimum_calendar_span_days: int = Field(ge=1)
    minimum_instruments: int = Field(ge=2)
    minimum_bars_per_instrument: int = Field(ge=2)
    minimum_eligible_instruments_per_date: int = Field(ge=2)
    required_price_adjustment: Literal["corporate_action_adjusted"]
    required_membership_semantics: Literal["exact_snapshot"]
    require_delisted_securities: Literal[True]
    require_local_research_license: Literal[True]
    selection_window: Literal["validation"]
    final_holdout_access: Literal[False]
    production_order_routing: Literal[False]

    @classmethod
    def load(cls, path: Path) -> DataReadinessConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchDataReadinessError(
                "research_data_readiness_config_invalid",
                "Research data-readiness configuration is invalid",
            ) from error


class PackageFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: Literal["csv"]


class ResearchDataPackageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    package_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    license_allows_local_research: Literal[True]
    acquired_at: datetime
    price_adjustment: Literal["corporate_action_adjusted"]
    membership_semantics: Literal["exact_snapshot"]
    includes_delisted_securities: Literal[True]
    prices: PackageFile
    universe: PackageFile

    @field_validator("acquired_at", mode="before")
    @classmethod
    def parse_acquired_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def timestamp_aware(self) -> ResearchDataPackageManifest:
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("package acquisition timestamp must be timezone-aware")
        if self.prices.path == self.universe.path:
            raise ValueError("price and universe artifacts must be distinct")
        return self

    @classmethod
    def load(cls, path: Path) -> ResearchDataPackageManifest:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchDataReadinessError(
                "research_data_package_manifest_invalid",
                "Research data-package manifest is invalid",
            ) from error


@dataclass(frozen=True, slots=True)
class DataReadinessMetrics:
    first_date: date
    last_date: date
    calendar_span_days: int
    instruments: int
    price_dates: int
    bars: int
    universe_observations: int
    minimum_bars_per_instrument: int
    minimum_eligible_instruments_per_date: int


@dataclass(frozen=True, slots=True)
class ValidatedResearchDataPackage:
    package_id: str
    provider: str
    license_id: str
    manifest_sha256: str
    prices_sha256: str
    universe_sha256: str
    package_sha256: str
    bars: tuple[AdjustedDailyBar, ...]
    universe: PointInTimeUniverse
    metrics: DataReadinessMetrics
    status: Literal["READY_FOR_QR14"] = "READY_FOR_QR14"
    selection_window: Literal["validation"] = "validation"
    final_holdout_access: Literal[False] = False
    final_holdout_consumed: Literal[False] = False
    production_order_routing: Literal[False] = False

    def receipt(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "package_id": self.package_id,
            "provider": self.provider,
            "license_id": self.license_id,
            "manifest_sha256": self.manifest_sha256,
            "prices_sha256": self.prices_sha256,
            "universe_sha256": self.universe_sha256,
            "package_sha256": self.package_sha256,
            "metrics": {
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(self.metrics).items()
            },
            "status": self.status,
            "selection_window": "validation",
            "final_holdout_access": False,
            "final_holdout_consumed": False,
            "production_order_routing": False,
        }


@dataclass(frozen=True, slots=True)
class ResearchDataPackageImporter:
    config: DataReadinessConfig

    def load(self, manifest_path: Path, *, project_root: Path) -> ValidatedResearchDataPackage:
        root = _resolve_directory(project_root)
        manifest_file = _resolve_file(manifest_path, root)
        manifest = ResearchDataPackageManifest.load(manifest_file)
        if (
            manifest.price_adjustment != self.config.required_price_adjustment
            or manifest.membership_semantics != self.config.required_membership_semantics
            or not manifest.includes_delisted_securities
            or not manifest.license_allows_local_research
        ):
            raise ResearchDataReadinessError(
                "research_data_package_policy_failed",
                "Data package does not satisfy adjustment, membership, or license policy",
            )
        prices_path = _resolve_file(root / manifest.prices.path, root)
        universe_path = _resolve_file(root / manifest.universe.path, root)
        _verify_checksum(prices_path, manifest.prices.sha256)
        _verify_checksum(universe_path, manifest.universe.sha256)
        bars = _load_prices(prices_path, manifest)
        universe = _load_universe(universe_path, manifest, root)
        metrics = _audit(bars, universe, self.config)
        manifest_sha = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        package_sha = _fingerprint(manifest, manifest_sha, metrics)
        return ValidatedResearchDataPackage(
            manifest.package_id,
            manifest.provider,
            manifest.license_id,
            manifest_sha,
            manifest.prices.sha256,
            manifest.universe.sha256,
            package_sha,
            bars,
            universe,
            metrics,
        )


def write_readiness_receipt(package: ValidatedResearchDataPackage, output_directory: Path) -> Path:
    path = output_directory / f"readiness-{package.package_sha256}.json"
    text = json.dumps(package.receipt(), indent=2, sort_keys=True) + "\n"
    try:
        output_directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise ResearchDataReadinessError(
                    "research_data_receipt_conflict",
                    "Existing readiness receipt conflicts with its fingerprint",
                )
            return path
        path.write_text(text, encoding="utf-8", newline="\n")
    except ResearchDataReadinessError:
        raise
    except OSError as error:
        raise ResearchDataReadinessError(
            "research_data_receipt_failed", "Readiness receipt could not be stored"
        ) from error
    return path


def _load_prices(path: Path, manifest: ResearchDataPackageManifest) -> tuple[AdjustedDailyBar, ...]:
    expected = {
        "instrument",
        "timestamp",
        "available_at",
        "adjusted_close",
        "volume",
    }
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if set(reader.fieldnames or ()) != expected:
                raise ResearchDataReadinessError(
                    "research_data_price_schema_invalid", "Price CSV schema is invalid"
                )
            bars = tuple(
                AdjustedDailyBar(
                    row["instrument"],
                    datetime.fromisoformat(row["timestamp"]),
                    datetime.fromisoformat(row["available_at"]),
                    Decimal(row["adjusted_close"]),
                    int(row["volume"]),
                    manifest.prices.path,
                    manifest.prices.sha256,
                )
                for row in reader
            )
    except ResearchDataReadinessError:
        raise
    except (OSError, ValueError, InvalidOperation, KeyError) as error:
        raise ResearchDataReadinessError(
            "research_data_price_parse_failed", "Price CSV could not be parsed"
        ) from error
    keys = tuple((bar.timestamp, bar.instrument) for bar in bars)
    if not bars or keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ResearchDataReadinessError(
            "research_data_price_order_invalid", "Price rows must be unique and ordered"
        )
    return bars


def _load_universe(
    path: Path, manifest: ResearchDataPackageManifest, root: Path
) -> PointInTimeUniverse:
    expected = {"observed_on", "instrument", "trading_symbol", "name", "isin"}
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if set(reader.fieldnames or ()) != expected:
                raise ResearchDataReadinessError(
                    "research_data_universe_schema_invalid", "Universe CSV schema is invalid"
                )
            rows = tuple(reader)
    except ResearchDataReadinessError:
        raise
    except OSError as error:
        raise ResearchDataReadinessError(
            "research_data_universe_parse_failed", "Universe CSV could not be parsed"
        ) from error
    if not rows:
        raise ResearchDataReadinessError("research_data_universe_empty", "Universe CSV is empty")
    by_date: dict[date, list[UniverseMember]] = {}
    try:
        for row in rows:
            observed = date.fromisoformat(row["observed_on"])
            by_date.setdefault(observed, []).append(
                UniverseMember(
                    row["instrument"],
                    row["trading_symbol"],
                    row["name"],
                    row["isin"] or None,
                )
            )
    except (ValueError, KeyError) as error:
        raise ResearchDataReadinessError(
            "research_data_universe_parse_failed", "Universe CSV could not be parsed"
        ) from error
    observations: list[UniverseObservation] = []
    previous: set[str] = set()
    for observed, raw_members in sorted(by_date.items()):
        members = tuple(sorted(raw_members, key=lambda item: item.instrument_key))
        current = {member.instrument_key for member in members}
        if len(current) != len(members) or any(
            not member.instrument_key.strip() for member in members
        ):
            raise ResearchDataReadinessError(
                "research_data_universe_duplicate", "Universe contains invalid duplicate members"
            )
        observations.append(
            UniverseObservation(
                observed,
                manifest.universe.path,
                manifest.universe.sha256,
                members,
                tuple(sorted(current - previous)),
                tuple(sorted(previous - current)),
            )
        )
        previous = current
    relative = path.relative_to(root).as_posix()
    universe_manifest = UniverseManifest(
        1,
        f"{manifest.package_id}-universe",
        "licensed_point_in_time_import",
        "exact_snapshot",
        manifest.acquired_at,
        observations[0].observed_on,
        observations[-1].observed_on,
        len(observations),
        relative,
        manifest.universe.sha256,
    )
    return PointInTimeUniverse(universe_manifest, tuple(observations))


def _audit(
    bars: tuple[AdjustedDailyBar, ...],
    universe: PointInTimeUniverse,
    config: DataReadinessConfig,
) -> DataReadinessMetrics:
    dates = sorted({bar.timestamp.date() for bar in bars})
    instruments = {bar.instrument for bar in bars}
    counts = Counter(bar.instrument for bar in bars)
    price_instruments_by_date: dict[date, set[str]] = {}
    for bar in bars:
        price_instruments_by_date.setdefault(bar.timestamp.date(), set()).add(bar.instrument)
    membership = {
        observation.observed_on: {member.instrument_key for member in observation.members}
        for observation in universe.observations
    }
    if any(day not in membership for day in dates):
        raise ResearchDataReadinessError(
            "research_data_universe_gap", "Every price date requires exact universe membership"
        )
    eligible_counts = [len(price_instruments_by_date[day] & membership[day]) for day in dates]
    span = (dates[-1] - dates[0]).days
    minimum_bars = min(counts.values())
    minimum_eligible = min(eligible_counts)
    if (
        span < config.minimum_calendar_span_days
        or len(instruments) < config.minimum_instruments
        or minimum_bars < config.minimum_bars_per_instrument
        or minimum_eligible < config.minimum_eligible_instruments_per_date
    ):
        raise ResearchDataReadinessError(
            "research_data_coverage_insufficient",
            "Data package does not meet span, instrument, history, or breadth requirements",
        )
    return DataReadinessMetrics(
        dates[0],
        dates[-1],
        span,
        len(instruments),
        len(dates),
        len(bars),
        len(universe.observations),
        minimum_bars,
        minimum_eligible,
    )


def _resolve_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ResearchDataReadinessError(
            "research_data_path_invalid", "Project root is invalid"
        ) from error
    if not resolved.is_dir():
        raise ResearchDataReadinessError("research_data_path_invalid", "Project root is invalid")
    return resolved


def _resolve_file(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise ResearchDataReadinessError(
            "research_data_path_invalid", "Data-package files must stay inside the project"
        ) from error
    if not resolved.is_file():
        raise ResearchDataReadinessError(
            "research_data_path_invalid", "Data-package files must stay inside the project"
        )
    return resolved


def _verify_checksum(path: Path, expected: str) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ResearchDataReadinessError(
            "research_data_checksum_mismatch", "Data-package checksum does not match"
        )


def _fingerprint(
    manifest: ResearchDataPackageManifest,
    manifest_sha256: str,
    metrics: DataReadinessMetrics,
) -> str:
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "manifest_sha256": manifest_sha256,
        "metrics": {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in asdict(metrics).items()
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
