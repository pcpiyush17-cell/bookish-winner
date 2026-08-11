"""Idempotent, rate-limited historical candle ingestion into Parquet."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from personal_quant.broker.contracts import BrokerError
from personal_quant.broker.sandbox import KiteClient
from personal_quant.clocks import Clock
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.market_calendar import MarketCalendar

MARKET_ZONE = ZoneInfo("Asia/Kolkata")
INTERVAL_MINUTES = {"minute": 1, "15minute": 15, "day": 1440}


class HistoricalDataError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HistoricalRequest:
    instrument_key: InstrumentKey
    instrument_token: InstrumentToken
    interval: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.interval not in INTERVAL_MINUTES:
            raise HistoricalDataError(
                "interval_unsupported", "Only minute, 15minute, and day are enabled"
            )
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in (self.start, self.end)
        ):
            raise HistoricalDataError("request_time_naive", "Request times must be timezone-aware")
        if self.start > self.end:
            raise HistoricalDataError("request_range_invalid", "Request start must not follow end")


@dataclass(frozen=True, slots=True)
class Candle:
    instrument_key: str
    instrument_token: int
    interval: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    oi: int | None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    status: str
    manifest_path: Path
    raw_rows: int
    curated_rows: int
    invalid_rows: int
    gaps: tuple[str, ...]


class HistoricalSource(Protocol):
    def fetch(self, request: HistoricalRequest) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class KiteHistoricalSource:
    client: KiteClient

    def fetch(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        try:
            return self.client.historical_data(
                int(request.instrument_token),
                request.start.astimezone(MARKET_ZONE),
                request.end.astimezone(MARKET_ZONE),
                request.interval,
                continuous=False,
                oi=True,
            )
        except Exception:
            raise BrokerError(
                "historical_download_failed", "Historical download failed; details were redacted"
            ) from None


@dataclass(slots=True)
class HistoricalRateLimiter:
    clock: Clock
    max_requests: int = 2
    window: timedelta = timedelta(seconds=1)
    _requests: deque[datetime] | None = None

    def __post_init__(self) -> None:
        if self.max_requests <= 0:
            raise ValueError("max_requests must be positive")
        self._requests = deque()

    def acquire(self) -> None:
        now = self.clock.now()
        cutoff = now - self.window
        requests = self._requests
        assert requests is not None
        while requests and requests[0] <= cutoff:
            requests.popleft()
        if len(requests) >= self.max_requests:
            raise HistoricalDataError("historical_rate_limit", "Historical request limit reached")
        requests.append(now)


@dataclass(frozen=True, slots=True)
class HistoricalIngestor:
    source: HistoricalSource
    limiter: HistoricalRateLimiter
    calendar: MarketCalendar
    root: Path
    clock: Clock

    def ingest(self, request: HistoricalRequest) -> IngestionResult:
        fingerprint = _request_fingerprint(request)
        manifest_path = self.root / "manifests" / f"{fingerprint}.json"
        if manifest_path.exists():
            return _result_from_manifest(manifest_path, "already_present")
        self.limiter.acquire()
        requested_at = self.clock.now().astimezone(UTC)
        raw_rows = self.source.fetch(request)
        batch_id = f"{requested_at:%Y%m%dT%H%M%S}-{fingerprint[:12]}"
        raw_path = (
            self.root
            / "raw"
            / "provider=zerodha"
            / "type=candles"
            / f"interval={request.interval}"
            / f"year={request.start.year}"
            / f"batch={batch_id}.parquet"
        )
        _write_parquet_once(raw_path, [_raw_record(row) for row in raw_rows])
        candles: list[Candle] = []
        invalid: list[dict[str, object]] = []
        for index, row in enumerate(raw_rows):
            try:
                candles.append(_validate_candle(row, request, self.calendar))
            except HistoricalDataError as error:
                invalid.append({"row": index, "rule": error.code, "message": str(error)})
        duplicates = len(candles) != len({item.timestamp for item in candles})
        if duplicates:
            invalid.append({"row": -1, "rule": "duplicate_candle", "message": "Duplicate keys"})
        gaps = _find_gaps(request, candles, self.calendar)
        if invalid:
            curated_path: Path | None = None
            quarantine = (
                self.root
                / "quarantine"
                / f"date={requested_at.date().isoformat()}"
                / "reason=invalid-candles"
                / f"{batch_id}.json"
            )
            _write_json_once(
                quarantine,
                {
                    "source": str(raw_path),
                    "count": len(invalid),
                    "severity": "error",
                    "action": "rejected",
                    "samples": invalid[:20],
                },
            )
            curated_rows = 0
            status = "quarantined"
        else:
            curated_path = (
                self.root
                / "curated"
                / "asset_class=equity"
                / "exchange=NSE"
                / f"interval={request.interval}"
                / f"year={request.start.year}"
                / f"version={batch_id}.parquet"
            )
            _write_parquet_once(
                curated_path,
                [_candle_record(item) for item in sorted(candles, key=lambda item: item.timestamp)],
            )
            curated_rows = len(candles)
            status = "complete_with_gaps" if gaps else "complete"
        manifest = {
            "schema_version": 1,
            "request_fingerprint": fingerprint,
            "requested_at": requested_at.isoformat(),
            "request": _request_record(request),
            "raw_path": str(raw_path),
            "raw_checksum_sha256": _checksum(raw_path),
            "curated_path": str(curated_path) if curated_path else None,
            "curated_checksum_sha256": _checksum(curated_path) if curated_path else None,
            "raw_rows": len(raw_rows),
            "curated_rows": curated_rows,
            "invalid_rows": len(invalid),
            "gaps": gaps,
            "status": status,
        }
        _write_json_once(manifest_path, manifest)
        return IngestionResult(
            status, manifest_path, len(raw_rows), curated_rows, len(invalid), tuple(gaps)
        )


def _validate_candle(
    raw: Mapping[str, object], request: HistoricalRequest, calendar: MarketCalendar
) -> Candle:
    try:
        timestamp = datetime.fromisoformat(str(raw["date"])).astimezone(MARKET_ZONE)
        values = {name: Decimal(str(raw[name])) for name in ("open", "high", "low", "close")}
        volume = int(str(raw["volume"]))
        oi = int(str(raw["oi"])) if raw.get("oi") is not None else None
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise HistoricalDataError("candle_malformed", "Candle fields are malformed") from error
    if any(value <= 0 for value in values.values()) or volume < 0 or (oi is not None and oi < 0):
        raise HistoricalDataError(
            "candle_numeric_invalid", "Prices must be positive and quantities non-negative"
        )
    if (
        values["high"] < max(values["open"], values["close"])
        or values["low"] > min(values["open"], values["close"])
        or values["low"] > values["high"]
    ):
        raise HistoricalDataError("candle_ohlc_invalid", "OHLC bounds are inconsistent")
    if not calendar.is_trading_day(timestamp.date()):
        raise HistoricalDataError("candle_session_invalid", "Candle is outside an exchange session")
    if request.interval in {"minute", "15minute"}:
        session = calendar.session_times(timestamp.date())
        assert session is not None
        if (
            timestamp.minute % INTERVAL_MINUTES[request.interval]
            or timestamp.second
            or not (session.market_open <= timestamp.time() < session.market_close)
        ):
            raise HistoricalDataError(
                "candle_alignment_invalid", "Candle is not aligned to its exchange interval"
            )
    return Candle(
        str(request.instrument_key),
        int(request.instrument_token),
        request.interval,
        timestamp.astimezone(UTC),
        values["open"],
        values["high"],
        values["low"],
        values["close"],
        volume,
        oi,
    )


def _find_gaps(
    request: HistoricalRequest, candles: list[Candle], calendar: MarketCalendar
) -> list[str]:
    actual = {item.timestamp.astimezone(MARKET_ZONE) for item in candles}
    expected: list[datetime] = []
    day = request.start.astimezone(MARKET_ZONE).date()
    final = request.end.astimezone(MARKET_ZONE).date()
    while day <= final:
        session = calendar.session_times(day)
        if session is not None:
            if request.interval == "day":
                expected.append(datetime.combine(day, time(0), MARKET_ZONE))
            else:
                cursor = datetime.combine(day, session.market_open, MARKET_ZONE)
                close = datetime.combine(day, session.market_close, MARKET_ZONE)
                while cursor < close:
                    expected.append(cursor)
                    cursor += timedelta(minutes=INTERVAL_MINUTES[request.interval])
        day += timedelta(days=1)
    return [item.isoformat() for item in expected if item not in actual]


def _raw_record(row: Mapping[str, object]) -> dict[str, object]:
    return {"payload_json": json.dumps(dict(row), sort_keys=True, default=str)}


def _candle_record(candle: Candle) -> dict[str, object]:
    raw = asdict(candle)
    for name in ("open", "high", "low", "close"):
        raw[name] = str(raw[name])
    return raw


def _request_record(request: HistoricalRequest) -> dict[str, object]:
    return {
        "instrument_key": str(request.instrument_key),
        "instrument_token": int(request.instrument_token),
        "interval": request.interval,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
    }


def _request_fingerprint(request: HistoricalRequest) -> str:
    return hashlib.sha256(json.dumps(_request_record(request), sort_keys=True).encode()).hexdigest()


def _write_parquet_once(path: Path, rows: list[dict[str, object]]) -> None:
    if path.exists():
        raise HistoricalDataError("immutable_path_exists", "Immutable Parquet path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    try:
        pq.write_table(pa.Table.from_pylist(rows), partial, compression="zstd")
        partial.replace(path)
    except Exception as error:
        partial.unlink(missing_ok=True)
        raise HistoricalDataError(
            "parquet_write_failed", "Parquet batch could not be written"
        ) from error


def _write_json_once(path: Path, payload: object) -> None:
    if path.exists():
        raise HistoricalDataError("immutable_path_exists", "Immutable metadata path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _result_from_manifest(path: Path, status: str) -> IngestionResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return IngestionResult(
        status,
        path,
        int(raw["raw_rows"]),
        int(raw["curated_rows"]),
        int(raw["invalid_rows"]),
        tuple(str(item) for item in raw["gaps"]),
    )
