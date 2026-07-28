from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from personal_quant.analytics import (
    AnalyticsError,
    DuckDBAnalytics,
    FeatureDefinition,
    FeatureRegistry,
    VerifiedDataset,
    default_feature_registry,
    materialize_features,
)
from personal_quant.clocks import SimulatedClock

from .test_historical import FakeSource, candle, ingestor, request


def dataset(tmp_path: Path, *, as_of: datetime) -> VerifiedDataset:
    rows = [
        candle("2026-07-27T00:00:00+05:30"),
        candle("2026-07-28T00:00:00+05:30"),
        candle("2026-07-29T00:00:00+05:30"),
    ]
    result = ingestor(tmp_path, FakeSource(rows), SimulatedClock(as_of)).ingest(
        request(datetime(2026, 7, 27, tzinfo=UTC), datetime(2026, 7, 29, tzinfo=UTC))
    )
    return VerifiedDataset.from_manifests([result.manifest_path], as_of=as_of)


def test_polars_loader_and_duckdb_enforce_as_of_cutoff(tmp_path: Path) -> None:
    cutoff = datetime(2026, 7, 28, 18, 29, tzinfo=UTC)
    verified = dataset(tmp_path, as_of=cutoff)
    frame = verified.scan().collect()
    assert frame.height == 2
    assert cast(datetime, frame["timestamp"].max()) <= cutoff
    with DuckDBAnalytics(verified) as analytics:
        assert analytics.query("SELECT count(*) FROM candles") == [(2,)]
        with pytest.raises(AnalyticsError, match="read-only"):
            analytics.query("DELETE FROM candles")


def test_features_are_deterministic_versioned_and_immutable(tmp_path: Path) -> None:
    cutoff = datetime(2026, 7, 29, 18, 29, tzinfo=UTC)
    verified = dataset(tmp_path, as_of=cutoff)
    registry = default_feature_registry()
    created = datetime(2026, 7, 29, 19, tzinfo=UTC)
    first = tmp_path / "features" / "one.parquet"
    second = tmp_path / "features" / "two.parquet"
    manifest = materialize_features(
        verified, registry, ["return_1", "sma_3"], first, created_at=created
    )
    materialize_features(verified, registry, ["return_1", "sma_3"], second, created_at=created)
    assert first.read_bytes() == second.read_bytes()
    assert pl.read_parquet(first).columns[-2:] == ["return_1", "sma_3"]
    assert manifest.is_file()
    with pytest.raises(AnalyticsError, match="immutable"):
        materialize_features(verified, registry, ["return_1"], first, created_at=created)


def test_registry_and_manifest_fail_closed(tmp_path: Path) -> None:
    registry = FeatureRegistry()
    definition = FeatureDefinition("x", "1", lambda: pl.lit(1).alias("x"))
    registry.register(definition)
    with pytest.raises(AnalyticsError, match="already"):
        registry.register(definition)
    with pytest.raises(AnalyticsError, match="not registered"):
        registry.definitions(["missing"])
    with pytest.raises(AnalyticsError, match="At least one"):
        VerifiedDataset.from_manifests([], as_of=datetime.now(UTC))
    with pytest.raises(AnalyticsError, match="timezone-aware"):
        VerifiedDataset.from_manifests([], as_of=datetime.now())
