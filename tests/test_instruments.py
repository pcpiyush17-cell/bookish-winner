from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from personal_quant.domain.identifiers import InstrumentKey, InstrumentToken
from personal_quant.instruments import InstrumentError, InstrumentSnapshotStore


def row(token: int = 1001, symbol: str = "INFY") -> dict[str, object]:
    return {
        "instrument_token": token,
        "exchange_token": str(token),
        "tradingsymbol": symbol,
        "name": symbol,
        "expiry": "",
        "strike": "0",
        "tick_size": "0.05",
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": "NSE",
        "exchange": "NSE",
        "isin": f"INE-{symbol}",
    }


def test_snapshot_is_immutable_checksummed_and_resolves_durable_key(tmp_path: Path) -> None:
    store = InstrumentSnapshotStore(tmp_path)
    first = store.save(
        rows=[row()], snapshot_date=date(2026, 7, 27), downloaded_at=datetime.now(UTC)
    )
    second = store.save(
        rows=[row(2002)], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC)
    )
    assert first.resolve_token(InstrumentKey("NSE:INFY")) == InstrumentToken(1001)
    assert second.resolve_token(InstrumentKey("NSE:INFY")) == InstrumentToken(2002)
    assert second.resolve_key(InstrumentToken(2002)) == InstrumentKey("NSE:INFY")
    directory = tmp_path / "provider=zerodha" / "date=2026-07-28"
    assert store.load(directory) == second
    assert (
        store.save(
            rows=[row(2002)], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC)
        )
        == second
    )


def test_changed_same_day_snapshot_is_rejected(tmp_path: Path) -> None:
    store = InstrumentSnapshotStore(tmp_path)
    store.save(rows=[row()], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC))
    with pytest.raises(InstrumentError, match="different immutable snapshot"):
        store.save(
            rows=[row(2002)], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("exchange", "BSE"), ("segment", "NFO"), ("instrument_type", "FUT")],
)
def test_non_nse_cash_instruments_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    invalid = row()
    invalid[field] = value
    with pytest.raises(InstrumentError, match="NSE cash equity"):
        InstrumentSnapshotStore(tmp_path).save(
            rows=[invalid], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC)
        )


def test_duplicates_empty_naive_time_and_tampering_are_rejected(tmp_path: Path) -> None:
    store = InstrumentSnapshotStore(tmp_path)
    with pytest.raises(InstrumentError, match="cannot be empty"):
        store.save(rows=[], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC))
    with pytest.raises(InstrumentError, match="timezone-aware"):
        store.save(rows=[row()], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now())
    with pytest.raises(InstrumentError, match="keys must be unique"):
        store.save(
            rows=[row(), row(2002)],
            snapshot_date=date(2026, 7, 28),
            downloaded_at=datetime.now(UTC),
        )
    store.save(rows=[row()], snapshot_date=date(2026, 7, 28), downloaded_at=datetime.now(UTC))
    directory = tmp_path / "provider=zerodha" / "date=2026-07-28"
    (directory / "nse.csv").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(InstrumentError, match="checksum"):
        store.load(directory)
