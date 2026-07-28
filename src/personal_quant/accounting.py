"""Append-only cash accounting and fill-derived long-only portfolio projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from personal_quant.broker.contracts import OrderSide
from personal_quant.domain.identifiers import BrokerOrderId, FillId, InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.storage.database import Database


class AccountingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class JournalType(StrEnum):
    OPENING_CASH = "opening_cash"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    PURCHASE = "purchase"
    SALE = "sale"
    COST = "cost"
    DIVIDEND = "dividend"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    REVERSAL = "reversal"


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: FillId
    broker_order_id: BrokerOrderId
    instrument_key: InstrumentKey
    side: OrderSide
    quantity: int
    price: Money
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.price.amount <= 0:
            raise AccountingError("fill_invalid", "Fill quantity and price must be positive")
        _aware_iso(self.occurred_at)


@dataclass(frozen=True, slots=True)
class FillCost:
    cost_entry_id: UUID
    component: str
    amount: Money
    cost_kind: str
    calculation_version: str

    def __post_init__(self) -> None:
        if (
            self.amount.amount <= 0
            or not self.component.strip()
            or not self.calculation_version.strip()
        ):
            raise AccountingError(
                "cost_entry_invalid", "Cost entry fields must be positive and complete"
            )
        if self.cost_kind not in {"estimate", "actual", "adjustment"}:
            raise AccountingError("cost_kind_invalid", "Cost kind is unsupported")


@dataclass(frozen=True, slots=True)
class Position:
    instrument_key: InstrumentKey
    quantity: int
    cost_basis: Money
    average_cost: Money
    realised_pnl: Money


@dataclass(frozen=True, slots=True)
class Valuation:
    instrument_key: InstrumentKey
    quantity: int
    mark_price: Money
    market_value: Money
    unrealised_pnl: Money
    realised_pnl: Money


@dataclass(frozen=True, slots=True)
class ReconciliationDifference:
    layer: str
    key: str
    local: str
    broker: str


@dataclass(frozen=True, slots=True)
class PortfolioAccounting:
    database: Database

    def apply_fill(self, fill: Fill, costs: tuple[FillCost, ...] = ()) -> bool:
        """Apply a broker fill and its costs atomically; exact duplicates are no-ops."""
        payload_hash = _fill_hash(fill)
        with self.database.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT payload_hash FROM fills WHERE fill_id = ?", (str(fill.fill_id),)
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise AccountingError(
                        "duplicate_fill_conflict", "Duplicate fill ID has different economics"
                    )
                return False
            current = connection.execute(
                "SELECT * FROM positions WHERE instrument_key = ?", (str(fill.instrument_key),)
            ).fetchone()
            quantity = int(current["quantity"]) if current else 0
            basis = int(current["cost_basis_paise"]) if current else 0
            realised = int(current["realised_pnl_paise"]) if current else 0
            price = _paise(fill.price)
            if fill.side is OrderSide.BUY:
                new_quantity = quantity + fill.quantity
                new_basis = basis + price * fill.quantity
                cash_change = -(price * fill.quantity)
                journal_type = JournalType.PURCHASE
            else:
                if fill.quantity > quantity:
                    raise AccountingError("short_sale_rejected", "Sell fill exceeds long position")
                removed_basis = (
                    basis if fill.quantity == quantity else basis * fill.quantity // quantity
                )
                proceeds = price * fill.quantity
                new_quantity = quantity - fill.quantity
                new_basis = basis - removed_basis
                realised += proceeds - removed_basis
                cash_change = proceeds
                journal_type = JournalType.SALE
            occurred = _aware_iso(fill.occurred_at)
            connection.execute(
                "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(fill.fill_id),
                    str(fill.broker_order_id),
                    str(fill.instrument_key),
                    fill.side.value,
                    fill.quantity,
                    price,
                    occurred,
                    payload_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO positions VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instrument_key) DO UPDATE SET
                    quantity=excluded.quantity, cost_basis_paise=excluded.cost_basis_paise,
                    realised_pnl_paise=excluded.realised_pnl_paise, updated_at=excluded.updated_at
                """,
                (str(fill.instrument_key), new_quantity, new_basis, realised, occurred),
            )
            connection.execute(
                """
                INSERT INTO cash_ledger(
                    entry_id, entry_type, amount_paise, instrument_key, fill_id, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fill:{fill.fill_id}",
                    journal_type.value,
                    cash_change,
                    str(fill.instrument_key),
                    str(fill.fill_id),
                    occurred,
                ),
            )
            for cost in costs:
                self._insert_cost(connection, fill, cost, occurred)
        return True

    def append_journal(
        self,
        *,
        entry_id: UUID,
        entry_type: JournalType,
        amount: Money,
        occurred_at: datetime,
        note: str = "",
        reversal_of: UUID | None = None,
    ) -> bool:
        if amount.amount == 0:
            raise AccountingError("journal_zero", "Journal amount cannot be zero")
        if entry_type is JournalType.REVERSAL and reversal_of is None:
            raise AccountingError("reversal_target_missing", "Reversal requires an original entry")
        try:
            with self.database.transaction(write=True) as connection:
                values = (
                    entry_type.value,
                    _paise(amount),
                    str(reversal_of) if reversal_of else None,
                    _aware_iso(occurred_at),
                    note,
                )
                existing = connection.execute(
                    """
                    SELECT entry_type, amount_paise, reversal_of, occurred_at, note
                    FROM cash_ledger WHERE entry_id = ?
                    """,
                    (str(entry_id),),
                ).fetchone()
                if existing is not None:
                    stored = tuple(existing)
                    if stored != values:
                        raise AccountingError(
                            "journal_entry_conflict",
                            "Journal entry ID has different accounting values",
                        )
                    return False
                cursor = connection.execute(
                    """
                    INSERT INTO cash_ledger(
                        entry_id, entry_type, amount_paise, reversal_of, occurred_at, note
                    ) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(entry_id) DO NOTHING
                    """,
                    (str(entry_id), *values),
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError as error:
            raise AccountingError(
                "journal_integrity", "Journal adjustment violates invariants"
            ) from error

    def position(self, key: InstrumentKey) -> Position | None:
        connection = self.database.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM positions WHERE instrument_key = ?", (str(key),)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        quantity = int(row["quantity"])
        basis = int(row["cost_basis_paise"])
        average = Decimal(basis) / Decimal(quantity * 100) if quantity else Decimal(0)
        return Position(
            key,
            quantity,
            _money_from_paise(basis),
            Money(average),
            _money_from_paise(int(row["realised_pnl_paise"])),
        )

    def cash_balance(self) -> Money:
        connection = self.database.connect(read_only=True)
        try:
            value = connection.execute(
                "SELECT COALESCE(SUM(amount_paise), 0) FROM cash_ledger"
            ).fetchone()[0]
        finally:
            connection.close()
        return _money_from_paise(int(value))

    def cost_total(self, *, cost_kind: str | None = None) -> Money:
        connection = self.database.connect(read_only=True)
        try:
            if cost_kind is None:
                value = connection.execute(
                    "SELECT COALESCE(SUM(amount_paise), 0) FROM cost_entries"
                ).fetchone()[0]
            else:
                value = connection.execute(
                    "SELECT COALESCE(SUM(amount_paise), 0) FROM cost_entries WHERE cost_kind = ?",
                    (cost_kind,),
                ).fetchone()[0]
        finally:
            connection.close()
        return _money_from_paise(int(value))

    def reconcile_cash(self, broker_cash: Money) -> tuple[ReconciliationDifference, ...]:
        local = self.cash_balance()
        if local == broker_cash:
            return ()
        return (
            ReconciliationDifference("cash", "INR", str(local.amount), str(broker_cash.amount)),
        )

    def value_position(self, key: InstrumentKey, mark: Money, valued_at: datetime) -> Valuation:
        position = self.position(key)
        if position is None:
            raise AccountingError("position_not_found", "Cannot value an unknown position")
        market_value = mark.multiply(position.quantity)
        valuation = Valuation(
            key,
            position.quantity,
            mark,
            market_value,
            market_value - position.cost_basis,
            position.realised_pnl,
        )
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO valuation_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(key),
                    position.quantity,
                    _paise(mark),
                    _paise(market_value),
                    _paise(valuation.unrealised_pnl),
                    _aware_iso(valued_at),
                ),
            )
        return valuation

    def reconcile_positions(
        self, broker: dict[InstrumentKey, tuple[int, Money]]
    ) -> tuple[ReconciliationDifference, ...]:
        connection = self.database.connect(read_only=True)
        try:
            rows = connection.execute("SELECT * FROM positions WHERE quantity != 0").fetchall()
        finally:
            connection.close()
        local = {
            InstrumentKey(row["instrument_key"]): (
                int(row["quantity"]),
                int(row["cost_basis_paise"]),
            )
            for row in rows
        }
        differences: list[ReconciliationDifference] = []
        for key in sorted(set(local) | set(broker), key=str):
            local_quantity, local_basis = local.get(key, (0, 0))
            broker_quantity, broker_average = broker.get(key, (0, Money.from_value(0)))
            broker_basis = _paise(broker_average) * broker_quantity
            if (local_quantity, local_basis) != (broker_quantity, broker_basis):
                differences.append(
                    ReconciliationDifference(
                        "positions",
                        str(key),
                        f"{local_quantity}@{local_basis}",
                        f"{broker_quantity}@{broker_basis}",
                    )
                )
        return tuple(differences)

    def _insert_cost(
        self, connection: sqlite3.Connection, fill: Fill, cost: FillCost, occurred: str
    ) -> None:
        amount = _paise(cost.amount)
        journal_id = f"cost:{cost.cost_entry_id}"
        connection.execute(
            """
            INSERT INTO cash_ledger(
                entry_id, entry_type, amount_paise, instrument_key, fill_id, occurred_at
            ) VALUES (?, 'cost', ?, ?, ?, ?)
            """,
            (journal_id, -amount, str(fill.instrument_key), str(fill.fill_id), occurred),
        )
        connection.execute(
            "INSERT INTO cost_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(cost.cost_entry_id),
                journal_id,
                str(fill.fill_id),
                cost.component,
                amount,
                cost.cost_kind,
                cost.calculation_version,
                occurred,
            ),
        )


def _fill_hash(fill: Fill) -> str:
    raw = {
        "broker_order_id": str(fill.broker_order_id),
        "instrument_key": str(fill.instrument_key),
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price_paise": _paise(fill.price),
        "occurred_at": _aware_iso(fill.occurred_at),
    }
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _paise(value: Money) -> int:
    return int((value.amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _money_from_paise(value: int) -> Money:
    return Money(Decimal(value) / 100)


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AccountingError(
            "accounting_time_naive", "Accounting timestamps must be timezone-aware"
        )
    return value.astimezone(UTC).isoformat()
