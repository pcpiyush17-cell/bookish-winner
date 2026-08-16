"""Point-in-time NSE research universes built from immutable instrument snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.instruments import InstrumentSnapshot, InstrumentSnapshotStore


class ResearchUniverseError(ValueError):
    """Point-in-time universe failure with a stable operator-facing code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class UniverseQualityPolicy(BaseModel):
    """Versioned fail-closed quality rules for research universe construction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    policy_id: str = Field(min_length=1)
    exchange: Literal["NSE"]
    segment: Literal["NSE"]
    instrument_type: Literal["EQ"]
    membership_semantics: Literal["exact_snapshot"]
    minimum_snapshots: int = Field(ge=1)
    minimum_members_per_snapshot: int = Field(ge=1)
    maximum_missing_isin_ratio: float = Field(ge=0, le=1)
    reject_duplicate_isin: bool
    reject_key_isin_change: bool

    @classmethod
    def load(cls, path: Path) -> UniverseQualityPolicy:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise ResearchUniverseError(
                "research_universe_policy_invalid", "Research universe policy is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class UniverseMember:
    instrument_key: str
    trading_symbol: str
    name: str
    isin: str | None


@dataclass(frozen=True, slots=True)
class UniverseObservation:
    observed_on: date
    source_manifest: str
    source_sha256: str
    members: tuple[UniverseMember, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UniverseManifest:
    schema_version: int
    universe_id: str
    policy_id: str
    membership_semantics: str
    created_at: datetime
    first_observation: date
    last_observation: date
    observation_count: int
    data_path: str
    data_sha256: str


@dataclass(frozen=True, slots=True)
class PointInTimeUniverse:
    manifest: UniverseManifest
    observations: tuple[UniverseObservation, ...]

    def members_on(self, observed_on: date) -> tuple[UniverseMember, ...]:
        """Return only exact-date membership; never backfill a missing observation."""
        for observation in self.observations:
            if observation.observed_on == observed_on:
                return observation.members
        raise ResearchUniverseError(
            "research_universe_date_unobserved",
            "No exact instrument snapshot exists for the requested universe date",
        )

    def contains(self, key: InstrumentKey, *, observed_on: date) -> bool:
        return str(key) in {member.instrument_key for member in self.members_on(observed_on)}


@dataclass(frozen=True, slots=True)
class PointInTimeUniverseStore:
    root: Path

    def build(
        self,
        *,
        snapshot_directories: tuple[Path, ...],
        policy: UniverseQualityPolicy,
        created_at: datetime,
        project_root: Path,
    ) -> PointInTimeUniverse:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ResearchUniverseError(
                "research_universe_time_naive", "Universe creation time must be timezone-aware"
            )
        if len(snapshot_directories) < policy.minimum_snapshots:
            raise ResearchUniverseError(
                "research_universe_history_short", "Too few dated snapshots for the quality policy"
            )
        snapshots = tuple(
            InstrumentSnapshotStore(directory).load(directory) for directory in snapshot_directories
        )
        _validate_snapshots(snapshots, policy)
        observations = _observations(snapshot_directories, snapshots, project_root)
        data_text = _data_json(observations)
        data_sha256 = hashlib.sha256(data_text.encode("utf-8")).hexdigest()
        source_fingerprint = "\n".join(
            f"{item.observed_on.isoformat()}:{item.source_sha256}" for item in observations
        )
        universe_id = (
            "nse-equity-"
            + hashlib.sha256(f"{policy.policy_id}\n{source_fingerprint}".encode()).hexdigest()[:16]
        )
        directory = self.root / universe_id
        data_path = directory / "universe.json"
        manifest_path = directory / "manifest.json"
        manifest = UniverseManifest(
            schema_version=1,
            universe_id=universe_id,
            policy_id=policy.policy_id,
            membership_semantics=policy.membership_semantics,
            created_at=created_at.astimezone(UTC),
            first_observation=observations[0].observed_on,
            last_observation=observations[-1].observed_on,
            observation_count=len(observations),
            data_path=_relative(data_path, project_root),
            data_sha256=data_sha256,
        )
        if data_path.exists() or manifest_path.exists():
            return self.load(manifest_path, project_root=project_root)
        directory.mkdir(parents=True, exist_ok=False)
        _write(data_path, data_text)
        _write(manifest_path, _manifest_json(manifest))
        return PointInTimeUniverse(manifest, observations)

    def load(self, manifest_path: Path, *, project_root: Path) -> PointInTimeUniverse:
        try:
            _validate_artifact_paths(
                project_root=project_root, store_root=self.root, manifest_path=manifest_path
            )
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = _parse_manifest(raw_manifest)
            data_path = (project_root / manifest.data_path).resolve()
            if not _is_within(data_path, project_root.resolve()):
                raise ResearchUniverseError(
                    "research_universe_path_escape", "Universe data path escapes the project root"
                )
            data_text = data_path.read_text(encoding="utf-8")
            if hashlib.sha256(data_text.encode("utf-8")).hexdigest() != manifest.data_sha256:
                raise ResearchUniverseError(
                    "research_universe_checksum", "Universe data checksum does not match"
                )
            observations = _parse_observations(json.loads(data_text))
        except ResearchUniverseError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ResearchUniverseError(
                "research_universe_read_failed", "Research universe cannot be read"
            ) from error
        if len(observations) != manifest.observation_count:
            raise ResearchUniverseError(
                "research_universe_observation_count", "Universe observation count does not match"
            )
        _validate_loaded_contract(manifest, observations)
        return PointInTimeUniverse(manifest, observations)


def discover_snapshot_directories(root: Path) -> tuple[Path, ...]:
    """Discover dated Zerodha snapshots without reading mutable operational state."""
    return tuple(sorted(path.parent for path in root.glob("provider=zerodha/date=*/manifest.json")))


def validate_cli_paths(*, project_root: Path, snapshots_root: Path, output_root: Path) -> None:
    """Restrict CLI reads and writes to the QR-00-approved research boundary."""
    root = project_root.resolve()
    reference_root = (root / "data/reference/instruments").resolve()
    research_root = (root / "research/data/universes").resolve()
    if not _is_within(snapshots_root.resolve(), reference_root):
        raise ResearchUniverseError(
            "research_universe_input_prohibited",
            "Universe snapshots must come from the instrument reference-data tree",
        )
    if not _is_within(output_root.resolve(), research_root):
        raise ResearchUniverseError(
            "research_universe_output_prohibited",
            "Universe artifacts must stay in the research universe tree",
        )


def _validate_snapshots(
    snapshots: tuple[InstrumentSnapshot, ...], policy: UniverseQualityPolicy
) -> None:
    dates = tuple(snapshot.manifest.snapshot_date for snapshot in snapshots)
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise ResearchUniverseError(
            "research_universe_dates_invalid", "Snapshot dates must be unique and increasing"
        )
    identity_by_key: dict[str, str] = {}
    for snapshot in snapshots:
        members = snapshot.instruments
        if len(members) < policy.minimum_members_per_snapshot:
            raise ResearchUniverseError(
                "research_universe_members_short", "A snapshot has too few eligible members"
            )
        if any(
            item.exchange != policy.exchange
            or item.segment != policy.segment
            or item.instrument_type != policy.instrument_type
            or not item.active
            for item in members
        ):
            raise ResearchUniverseError(
                "research_universe_scope_invalid", "Universe contains an ineligible instrument"
            )
        missing = sum(item.isin is None for item in members)
        if missing / len(members) > policy.maximum_missing_isin_ratio:
            raise ResearchUniverseError(
                "research_universe_isin_missing", "Missing ISIN ratio exceeds the quality policy"
            )
        isins = [item.isin for item in members if item.isin]
        if policy.reject_duplicate_isin and len(isins) != len(set(isins)):
            raise ResearchUniverseError(
                "research_universe_isin_duplicate", "A snapshot contains duplicate ISIN values"
            )
        for item in members:
            if not item.isin:
                continue
            key = str(item.key)
            previous = identity_by_key.setdefault(key, item.isin)
            if policy.reject_key_isin_change and previous != item.isin:
                raise ResearchUniverseError(
                    "research_universe_identity_changed",
                    "A durable instrument key changed ISIN across observations",
                )


def _observations(
    directories: tuple[Path, ...],
    snapshots: tuple[InstrumentSnapshot, ...],
    project_root: Path,
) -> tuple[UniverseObservation, ...]:
    result: list[UniverseObservation] = []
    previous: set[str] = set()
    for directory, snapshot in zip(directories, snapshots, strict=True):
        members = tuple(
            UniverseMember(str(item.key), item.trading_symbol, item.name, item.isin)
            for item in snapshot.instruments
        )
        current = {item.instrument_key for item in members}
        result.append(
            UniverseObservation(
                observed_on=snapshot.manifest.snapshot_date,
                source_manifest=_relative(directory / "manifest.json", project_root),
                source_sha256=snapshot.manifest.checksum_sha256,
                members=members,
                added=tuple(sorted(current - previous)),
                removed=tuple(sorted(previous - current)),
            )
        )
        previous = current
    return tuple(result)


def _data_json(observations: tuple[UniverseObservation, ...]) -> str:
    raw = []
    for observation in observations:
        item = asdict(observation)
        item["observed_on"] = observation.observed_on.isoformat()
        raw.append(item)
    return json.dumps({"observations": raw}, sort_keys=True, separators=(",", ":")) + "\n"


def _manifest_json(manifest: UniverseManifest) -> str:
    raw = asdict(manifest)
    for key in ("created_at", "first_observation", "last_observation"):
        raw[key] = raw[key].isoformat()
    return json.dumps(raw, sort_keys=True, indent=2) + "\n"


def _parse_manifest(raw: dict[str, object]) -> UniverseManifest:
    return UniverseManifest(
        schema_version=int(str(raw["schema_version"])),
        universe_id=str(raw["universe_id"]),
        policy_id=str(raw["policy_id"]),
        membership_semantics=str(raw["membership_semantics"]),
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        first_observation=date.fromisoformat(str(raw["first_observation"])),
        last_observation=date.fromisoformat(str(raw["last_observation"])),
        observation_count=int(str(raw["observation_count"])),
        data_path=str(raw["data_path"]),
        data_sha256=str(raw["data_sha256"]),
    )


def _parse_observations(raw: dict[str, object]) -> tuple[UniverseObservation, ...]:
    items = raw["observations"]
    if not isinstance(items, list):
        raise ValueError("observations must be a list")
    return tuple(
        UniverseObservation(
            observed_on=date.fromisoformat(str(item["observed_on"])),
            source_manifest=str(item["source_manifest"]),
            source_sha256=str(item["source_sha256"]),
            members=tuple(UniverseMember(**member) for member in item["members"]),
            added=tuple(str(value) for value in item["added"]),
            removed=tuple(str(value) for value in item["removed"]),
        )
        for item in items
    )


def _validate_loaded_contract(
    manifest: UniverseManifest, observations: tuple[UniverseObservation, ...]
) -> None:
    dates = tuple(item.observed_on for item in observations)
    if (
        manifest.schema_version != 1
        or manifest.membership_semantics != "exact_snapshot"
        or len(manifest.data_sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest.data_sha256)
    ):
        raise ResearchUniverseError(
            "research_universe_manifest_invalid", "Universe manifest contract is invalid"
        )
    if not observations or dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise ResearchUniverseError(
            "research_universe_observations_invalid", "Universe observations are not ordered"
        )
    if dates[0] != manifest.first_observation or dates[-1] != manifest.last_observation:
        raise ResearchUniverseError(
            "research_universe_date_range", "Universe manifest date range does not match"
        )
    previous: set[str] = set()
    for observation in observations:
        current = {member.instrument_key for member in observation.members}
        if len(current) != len(observation.members):
            raise ResearchUniverseError(
                "research_universe_member_duplicate", "Universe observation has duplicate members"
            )
        if observation.added != tuple(sorted(current - previous)) or observation.removed != tuple(
            sorted(previous - current)
        ):
            raise ResearchUniverseError(
                "research_universe_transition_invalid",
                "Universe transitions do not match membership",
            )
        previous = current


def _validate_artifact_paths(*, project_root: Path, store_root: Path, manifest_path: Path) -> None:
    root = project_root.resolve()
    resolved_store = store_root.resolve()
    resolved_manifest = manifest_path.resolve()
    approved = (root / "research/data/universes").resolve()
    if (
        not _is_within(resolved_store, approved)
        or not _is_within(resolved_manifest, resolved_store)
        or resolved_manifest.name != "manifest.json"
    ):
        raise ResearchUniverseError(
            "research_universe_artifact_prohibited",
            "Universe manifest must stay in the research universe tree",
        )


def _relative(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as error:
        raise ResearchUniverseError(
            "research_universe_path_escape", "Universe artifact escapes the project root"
        ) from error


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _write(path: Path, content: str) -> None:
    partial = path.with_suffix(path.suffix + ".partial")
    try:
        partial.write_text(content, encoding="utf-8", newline="\n")
        partial.replace(path)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise ResearchUniverseError(
            "research_universe_write_failed", "Universe artifact could not be written"
        ) from error
