"""Point-in-time-safe analytical access and deterministic feature materialization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl


class AnalyticsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VerifiedDataset:
    paths: tuple[Path, ...]
    manifest_checksums: tuple[str, ...]
    as_of: datetime

    @classmethod
    def from_manifests(cls, manifest_paths: Iterable[Path], *, as_of: datetime) -> VerifiedDataset:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise AnalyticsError("as_of_naive", "Analytics cutoff must be timezone-aware")
        files: list[Path] = []
        hashes: list[str] = []
        for manifest_path in sorted(manifest_paths):
            try:
                content = manifest_path.read_bytes()
                raw = json.loads(content)
                if raw["status"] not in {"complete", "complete_with_gaps"}:
                    raise AnalyticsError("manifest_not_curated", "Manifest has no curated data")
                path = Path(str(raw["curated_path"]))
                expected = str(raw["curated_checksum_sha256"])
                if not path.is_file() or _checksum(path) != expected:
                    raise AnalyticsError("curated_checksum", "Curated Parquet checksum failed")
            except AnalyticsError:
                raise
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise AnalyticsError("manifest_invalid", "Analytics manifest is invalid") from error
            files.append(path)
            hashes.append(hashlib.sha256(content).hexdigest())
        if not files:
            raise AnalyticsError("dataset_empty", "At least one verified manifest is required")
        return cls(tuple(files), tuple(hashes), as_of.astimezone(UTC))

    def scan(self) -> pl.LazyFrame:
        frame = pl.scan_parquet([str(path) for path in self.paths], hive_partitioning=False)
        required = {
            "instrument_key",
            "instrument_token",
            "interval",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "oi",
        }
        if not required.issubset(frame.collect_schema().names()):
            raise AnalyticsError("curated_schema", "Curated candle schema is incomplete")
        frame = frame.filter(pl.col("timestamp") <= self.as_of).sort(
            ["instrument_key", "interval", "timestamp"]
        )
        keys = ["instrument_key", "interval", "timestamp"]
        if frame.select(pl.struct(keys).is_duplicated().any()).collect().item():
            raise AnalyticsError("curated_duplicates", "Curated candle keys are duplicated")
        return frame


@dataclass(slots=True)
class DuckDBAnalytics:
    dataset: VerifiedDataset
    _connection: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> DuckDBAnalytics:
        connection = duckdb.connect(":memory:")
        paths = [str(path) for path in self.dataset.paths]
        cutoff = self.dataset.as_of.isoformat()
        connection.read_parquet(paths, hive_partitioning=False).filter(
            f"timestamp <= TIMESTAMPTZ '{cutoff}'"
        ).create_view("candles")
        self._connection = connection
        return self

    def query(self, sql: str, parameters: list[object] | None = None) -> list[tuple[object, ...]]:
        normalized = sql.strip().lower()
        if not normalized.startswith(("select ", "with ")) or ";" in normalized:
            raise AnalyticsError("query_not_read_only", "Analytics SQL must be one read-only query")
        if self._connection is None:
            raise AnalyticsError("analytics_closed", "DuckDB analytics context is not open")
        return self._connection.execute(sql, parameters or []).fetchall()

    def __exit__(self, *_: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


FeatureExpression = Callable[[], pl.Expr]


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    version: str
    expression: FeatureExpression


class FeatureRegistry:
    def __init__(self) -> None:
        self._features: dict[str, FeatureDefinition] = {}

    def register(self, definition: FeatureDefinition) -> None:
        if definition.name in self._features:
            raise AnalyticsError("feature_duplicate", "Feature name is already registered")
        self._features[definition.name] = definition

    def definitions(self, names: Iterable[str]) -> tuple[FeatureDefinition, ...]:
        try:
            return tuple(self._features[name] for name in names)
        except KeyError as error:
            raise AnalyticsError(
                "feature_unknown", "Requested feature is not registered"
            ) from error

    def fingerprint(self, names: Iterable[str]) -> str:
        values = [f"{item.name}:{item.version}" for item in self.definitions(sorted(names))]
        return hashlib.sha256("\n".join(values).encode()).hexdigest()


def default_feature_registry() -> FeatureRegistry:
    registry = FeatureRegistry()
    over_symbol = ["instrument_key", "interval"]
    registry.register(
        FeatureDefinition(
            "return_1",
            "1.0.0",
            lambda: (
                pl.col("close").cast(pl.Decimal(38, 10))
                / pl.col("close").cast(pl.Decimal(38, 10)).shift(1).over(over_symbol)
                - 1
            ).alias("return_1"),
        )
    )
    registry.register(
        FeatureDefinition(
            "sma_3",
            "1.0.0",
            lambda: (
                pl.col("close")
                .cast(pl.Decimal(38, 10))
                .rolling_mean(3)
                .over(over_symbol)
                .alias("sma_3")
            ),
        )
    )
    return registry


def materialize_features(
    dataset: VerifiedDataset,
    registry: FeatureRegistry,
    names: Iterable[str],
    output: Path,
    *,
    created_at: datetime,
) -> Path:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise AnalyticsError("feature_time_naive", "Feature creation time must be timezone-aware")
    selected = tuple(names)
    definitions = registry.definitions(selected)
    frame = dataset.scan().with_columns([item.expression() for item in definitions]).collect()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise AnalyticsError("feature_output_exists", "Feature output is immutable")
    frame.write_parquet(output, compression="zstd")
    manifest = {
        "schema_version": 1,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "as_of": dataset.as_of.isoformat(),
        "input_manifest_checksums": dataset.manifest_checksums,
        "feature_registry_fingerprint": registry.fingerprint(selected),
        "features": [{"name": item.name, "version": item.version} for item in definitions],
        "row_count": frame.height,
        "output_path": str(output),
        "output_checksum_sha256": _checksum(output),
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
