import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.historical import (
    HistoricalDataError,
    HistoricalIngestor,
    HistoricalRateLimiter,
    HistoricalRequest,
)
from personal_quant.market_calendar import MarketCalendar


class FakeSource:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls = 0

    def fetch(self, request: HistoricalRequest) -> list[dict[str, object]]:
        self.calls += 1
        return self.rows


def candle(day: str = "2026-07-28T00:00:00+05:30") -> dict[str, object]:
    return {
        "date": day,
        "open": 1500,
        "high": 1520,
        "low": 1490,
        "close": 1510,
        "volume": 1000,
        "oi": 0,
    }


def request(
    start: datetime, end: datetime | None = None, interval: str = "day"
) -> HistoricalRequest:
    return HistoricalRequest(
        InstrumentKey("NSE:INFY"), InstrumentToken(1001), interval, start, end or start
    )


def ingestor(tmp_path: Path, source: FakeSource, clock: SimulatedClock) -> HistoricalIngestor:
    return HistoricalIngestor(
        source,
        HistoricalRateLimiter(clock),
        MarketCalendar.load(Path("config/calendars/nse_equity_2026.yaml")),
        tmp_path / "data",
        clock,
    )


def test_ingestion_writes_raw_curated_manifest_and_is_idempotent(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    source = FakeSource([candle()])
    service = ingestor(tmp_path, source, SimulatedClock(now))
    first = service.ingest(request(now))
    second = service.ingest(request(now))
    assert first.status == "complete"
    assert second.status == "already_present"
    assert source.calls == 1
    parquet = next((tmp_path / "data" / "curated").rglob("*.parquet"))
    assert pq.ParquetFile(parquet).read().num_rows == 1
    assert first.manifest_path.is_file()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["calendar_id"] == "nse_equity_2026_v2"


def test_invalid_batch_is_quarantined_without_curated_output(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    invalid = candle()
    invalid["high"] = 1400
    result = ingestor(tmp_path, FakeSource([invalid]), SimulatedClock(now)).ingest(request(now))
    assert result.status == "quarantined"
    assert result.invalid_rows == 1
    assert not (tmp_path / "data" / "curated").exists()
    assert next((tmp_path / "data" / "quarantine").rglob("*.json")).is_file()


def test_missing_candles_are_reported_not_filled(tmp_path: Path) -> None:
    start = datetime(2026, 7, 27, tzinfo=UTC)
    end = datetime(2026, 7, 28, tzinfo=UTC)
    result = ingestor(tmp_path, FakeSource([candle()]), SimulatedClock(end)).ingest(
        request(start, end)
    )
    assert result.status == "complete_with_gaps"
    assert len(result.gaps) == 1


def test_rate_limiter_is_bounded_and_releases_old_requests() -> None:
    clock = SimulatedClock(datetime(2026, 7, 28, tzinfo=UTC))
    limiter = HistoricalRateLimiter(clock)
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(HistoricalDataError, match="limit"):
        limiter.acquire()
    clock.advance(timedelta(seconds=1))
    limiter.acquire()


def test_request_rejects_unknown_interval_naive_and_reverse_ranges() -> None:
    aware = datetime(2026, 7, 28, tzinfo=UTC)
    with pytest.raises(HistoricalDataError, match="enabled"):
        request(aware, interval="second")
    with pytest.raises(HistoricalDataError, match="timezone-aware"):
        request(datetime(2026, 7, 28))
    with pytest.raises(HistoricalDataError, match="must not follow"):
        request(aware, aware - timedelta(days=1))
