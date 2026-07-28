"""Immutable daily instrument snapshots and durable key resolution."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Any, Protocol

from personal_quant.broker.contracts import BrokerError
from personal_quant.broker.sandbox import KiteClient
from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken


class InstrumentError(ValueError):
    """An instrument validation or snapshot failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Instrument:
    key: InstrumentKey
    instrument_token: InstrumentToken
    exchange_token: str
    exchange: str
    segment: str
    trading_symbol: str
    name: str
    isin: str | None
    lot_size: int
    tick_size: Decimal
    instrument_type: str
    expiry: date | None
    strike: Decimal | None
    active: bool = True


@dataclass(frozen=True, slots=True)
class InstrumentManifest:
    schema_version: int
    provider: str
    exchange: str
    snapshot_date: date
    downloaded_at: datetime
    checksum_sha256: str
    row_count: int
    source: str


@dataclass(frozen=True, slots=True)
class InstrumentSnapshot:
    manifest: InstrumentManifest
    instruments: tuple[Instrument, ...]

    def __post_init__(self) -> None:
        if not self.instruments:
            raise InstrumentError("empty_snapshot", "Instrument snapshot cannot be empty")
        keys = [instrument.key for instrument in self.instruments]
        tokens = [instrument.instrument_token for instrument in self.instruments]
        if len(keys) != len(set(keys)):
            raise InstrumentError("duplicate_instrument_key", "Instrument keys must be unique")
        if len(tokens) != len(set(tokens)):
            raise InstrumentError("duplicate_instrument_token", "Instrument tokens must be unique")
        if self.manifest.row_count != len(self.instruments):
            raise InstrumentError(
                "manifest_row_count", "Manifest row count does not match snapshot"
            )

    def resolve_token(self, key: InstrumentKey) -> InstrumentToken:
        for instrument in self.instruments:
            if instrument.key == key and instrument.active:
                return instrument.instrument_token
        raise InstrumentError(
            "instrument_not_found", f"No active instrument for durable key: {key}"
        )

    def resolve_key(self, token: InstrumentToken) -> InstrumentKey:
        for instrument in self.instruments:
            if instrument.instrument_token == token and instrument.active:
                return instrument.key
        raise InstrumentError("instrument_token_not_found", "No active durable key for token")


class InstrumentSource(Protocol):
    def fetch_nse(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SandboxKiteInstrumentSource:
    client: KiteClient

    def fetch_nse(self) -> list[dict[str, Any]]:
        try:
            return self.client.instruments("NSE")
        except Exception:
            raise BrokerError(
                "sandbox_instruments_failed",
                "Sandbox instrument download failed; sensitive details were redacted",
            ) from None


@dataclass(frozen=True, slots=True)
class InstrumentSnapshotStore:
    root: Path

    def save(
        self,
        *,
        rows: Iterable[Mapping[str, object]],
        snapshot_date: date,
        downloaded_at: datetime,
    ) -> InstrumentSnapshot:
        """Validate and atomically persist one immutable normalized daily snapshot."""
        instruments = tuple(
            sorted((_parse_row(row) for row in rows), key=lambda item: str(item.key))
        )
        csv_text = _canonical_csv(instruments)
        checksum = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
        manifest = InstrumentManifest(
            schema_version=1,
            provider="zerodha",
            exchange="NSE",
            snapshot_date=snapshot_date,
            downloaded_at=_aware_utc(downloaded_at),
            checksum_sha256=checksum,
            row_count=len(instruments),
            source="kite-connect-instruments/NSE",
        )
        snapshot = InstrumentSnapshot(manifest, instruments)
        directory = self.root / "provider=zerodha" / f"date={snapshot_date.isoformat()}"
        csv_path = directory / "nse.csv"
        manifest_path = directory / "manifest.json"
        if csv_path.exists() or manifest_path.exists():
            existing = self.load(directory)
            if existing.manifest.checksum_sha256 != checksum:
                raise InstrumentError(
                    "snapshot_conflict",
                    "A different immutable snapshot already exists for this date",
                )
            return existing

        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(csv_path, csv_text)
        _atomic_write(manifest_path, _manifest_json(manifest))
        return snapshot

    def load(self, directory: Path) -> InstrumentSnapshot:
        try:
            csv_text = (directory / "nse.csv").read_text(encoding="utf-8")
            raw_manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InstrumentError(
                "snapshot_read_failed", "Instrument snapshot cannot be read"
            ) from error
        checksum = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
        if checksum != raw_manifest.get("checksum_sha256"):
            raise InstrumentError(
                "snapshot_checksum", "Instrument snapshot checksum does not match"
            )
        manifest = InstrumentManifest(
            schema_version=int(raw_manifest["schema_version"]),
            provider=str(raw_manifest["provider"]),
            exchange=str(raw_manifest["exchange"]),
            snapshot_date=date.fromisoformat(raw_manifest["snapshot_date"]),
            downloaded_at=datetime.fromisoformat(raw_manifest["downloaded_at"]),
            checksum_sha256=checksum,
            row_count=int(raw_manifest["row_count"]),
            source=str(raw_manifest["source"]),
        )
        rows = csv.DictReader(StringIO(csv_text))
        return InstrumentSnapshot(manifest, tuple(_parse_row(row) for row in rows))


def download_instruments(
    source: InstrumentSource,
    store: InstrumentSnapshotStore,
    *,
    snapshot_date: date,
    downloaded_at: datetime,
) -> InstrumentSnapshot:
    return store.save(
        rows=source.fetch_nse(), snapshot_date=snapshot_date, downloaded_at=downloaded_at
    )


def _parse_row(raw: Mapping[str, object]) -> Instrument:
    try:
        exchange = _required(raw, "exchange")
        segment = _required(raw, "segment")
        symbol = _required(raw, "tradingsymbol")
        instrument_type = _required(raw, "instrument_type")
        token = int(_required(raw, "instrument_token"))
        exchange_token = _required(raw, "exchange_token")
        lot_size = int(_required(raw, "lot_size"))
        tick_size = Decimal(_required(raw, "tick_size"))
        expiry_text = _optional(raw, "expiry")
        strike_text = _optional(raw, "strike")
    except (InvalidOperation, ValueError, TypeError) as error:
        raise InstrumentError(
            "instrument_row_invalid", "Instrument row contains invalid values"
        ) from error
    if exchange != "NSE" or segment != "NSE" or instrument_type != "EQ":
        raise InstrumentError("instrument_scope_invalid", "WP-04 accepts NSE cash equity rows only")
    if token <= 0 or lot_size <= 0 or tick_size <= 0:
        raise InstrumentError(
            "instrument_numeric_invalid", "Token, lot size, and tick size must be positive"
        )
    try:
        expiry = date.fromisoformat(expiry_text) if expiry_text else None
        parsed_strike = Decimal(strike_text) if strike_text else Decimal(0)
    except (InvalidOperation, ValueError) as error:
        raise InstrumentError(
            "instrument_row_invalid", "Instrument row contains invalid values"
        ) from error
    strike = parsed_strike if parsed_strike != 0 else None
    isin = _optional(raw, "isin")
    return Instrument(
        key=InstrumentKey(f"{exchange}:{symbol}"),
        instrument_token=InstrumentToken(token),
        exchange_token=exchange_token,
        exchange=exchange,
        segment=segment,
        trading_symbol=symbol,
        name=_required(raw, "name"),
        isin=isin,
        lot_size=lot_size,
        tick_size=tick_size,
        instrument_type=instrument_type,
        expiry=expiry,
        strike=strike,
        active=_optional(raw, "active").lower() not in {"false", "0", "no"},
    )


def _canonical_csv(instruments: tuple[Instrument, ...]) -> str:
    output = StringIO(newline="")
    fields = [
        "instrument_token",
        "exchange_token",
        "tradingsymbol",
        "name",
        "expiry",
        "strike",
        "tick_size",
        "lot_size",
        "instrument_type",
        "segment",
        "exchange",
        "isin",
        "active",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for item in instruments:
        writer.writerow(
            {
                "instrument_token": int(item.instrument_token),
                "exchange_token": item.exchange_token,
                "tradingsymbol": item.trading_symbol,
                "name": item.name,
                "expiry": item.expiry.isoformat() if item.expiry else "",
                "strike": str(item.strike or Decimal(0)),
                "tick_size": str(item.tick_size),
                "lot_size": item.lot_size,
                "instrument_type": item.instrument_type,
                "segment": item.segment,
                "exchange": item.exchange,
                "isin": item.isin or "",
                "active": str(item.active).lower(),
            }
        )
    return output.getvalue()


def _manifest_json(manifest: InstrumentManifest) -> str:
    raw = asdict(manifest)
    raw["snapshot_date"] = manifest.snapshot_date.isoformat()
    raw["downloaded_at"] = manifest.downloaded_at.isoformat()
    return json.dumps(raw, sort_keys=True, indent=2) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    partial = path.with_suffix(f"{path.suffix}.partial")
    try:
        partial.write_text(content, encoding="utf-8", newline="\n")
        partial.replace(path)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise InstrumentError(
            "snapshot_write_failed", "Instrument snapshot could not be written"
        ) from error


def _required(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if value is None or not str(value).strip():
        raise InstrumentError("instrument_field_missing", f"Instrument field is required: {key}")
    return str(value).strip()


def _optional(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    return "" if value is None else str(value).strip()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InstrumentError("snapshot_time_naive", "Snapshot time must be timezone-aware")
    return value.astimezone(UTC)
