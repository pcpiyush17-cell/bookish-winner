from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from personal_quant.accounting import (
    AccountingError,
    Fill,
    FillCost,
    JournalType,
    PortfolioAccounting,
)
from personal_quant.broker.contracts import OrderSide
from personal_quant.domain.identifiers import BrokerOrderId, FillId, InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner


def service(tmp_path: Path) -> PortfolioAccounting:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    return PortfolioAccounting(database)


def fill(identifier: str, side: OrderSide, quantity: int, price: str) -> Fill:
    return Fill(
        FillId(identifier),
        BrokerOrderId(f"order-{identifier}"),
        InstrumentKey("NSE:INFY"),
        side,
        quantity,
        Money.from_value(price),
        datetime(2026, 7, 29, 10, tzinfo=UTC),
    )


def test_complete_trade_cycle_balances_and_duplicate_is_idempotent(tmp_path: Path) -> None:
    accounting = service(tmp_path)
    opening_id = uuid4()
    assert accounting.append_journal(
        entry_id=opening_id,
        entry_type=JournalType.OPENING_CASH,
        amount=Money.from_value("10000"),
        occurred_at=datetime(2026, 7, 29, 3, 30, tzinfo=UTC),
    )
    buy = fill("fill-buy", OrderSide.BUY, 100, "10")
    cost = FillCost(
        uuid4(),
        "exchange_transaction_charge",
        Money.from_value("1.25"),
        "estimate",
        "zerodha_nse_delivery_2026-07-28_v1",
    )
    assert accounting.apply_fill(buy, (cost,))
    assert not accounting.apply_fill(buy, (cost,))
    position = accounting.position(InstrumentKey("NSE:INFY"))
    assert position is not None
    assert position.quantity == 100
    assert position.cost_basis == Money.from_value("1000")
    assert position.average_cost == Money.from_value("10")
    assert accounting.cash_balance() == Money.from_value("8998.75")
    assert accounting.cost_total(cost_kind="estimate") == Money.from_value("1.25")

    assert accounting.apply_fill(fill("fill-sell", OrderSide.SELL, 100, "12"))
    closed = accounting.position(InstrumentKey("NSE:INFY"))
    assert closed is not None
    assert closed.quantity == 0
    assert closed.realised_pnl == Money.from_value("200")
    assert accounting.cash_balance() == Money.from_value("10198.75")
    assert accounting.reconcile_cash(Money.from_value("10198.75")) == ()
    assert accounting.reconcile_cash(Money.from_value("1"))[0].layer == "cash"


def test_partial_sale_preserves_cost_basis_and_values_position(tmp_path: Path) -> None:
    accounting = service(tmp_path)
    accounting.apply_fill(fill("buy-1", OrderSide.BUY, 10, "100"))
    accounting.apply_fill(fill("buy-2", OrderSide.BUY, 10, "120"))
    accounting.apply_fill(fill("sell-1", OrderSide.SELL, 5, "130"))
    position = accounting.position(InstrumentKey("NSE:INFY"))
    assert position is not None
    assert position.quantity == 15
    assert position.cost_basis == Money.from_value("1650")
    assert position.average_cost == Money.from_value("110")
    assert position.realised_pnl == Money.from_value("100")
    valuation = accounting.value_position(
        InstrumentKey("NSE:INFY"),
        Money.from_value("125"),
        datetime(2026, 7, 29, 11, tzinfo=UTC),
    )
    assert valuation.market_value == Money.from_value("1875")
    assert valuation.unrealised_pnl == Money.from_value("225")


def test_duplicate_conflict_and_short_sale_fail_without_partial_writes(tmp_path: Path) -> None:
    accounting = service(tmp_path)
    original = fill("same", OrderSide.BUY, 2, "10")
    accounting.apply_fill(original)
    with pytest.raises(AccountingError, match="different economics"):
        accounting.apply_fill(fill("same", OrderSide.BUY, 3, "10"))
    with pytest.raises(AccountingError, match="exceeds"):
        accounting.apply_fill(fill("oversell", OrderSide.SELL, 3, "11"))
    assert accounting.position(InstrumentKey("NSE:INFY")).quantity == 2  # type: ignore[union-attr]
    assert accounting.cash_balance() == Money.from_value("-20")


def test_adjustments_are_append_only_reversible_and_idempotent(tmp_path: Path) -> None:
    accounting = service(tmp_path)
    original = uuid4()
    reversed_entry = uuid4()
    now = datetime(2026, 7, 29, 10, tzinfo=UTC)
    assert accounting.append_journal(
        entry_id=original,
        entry_type=JournalType.DEPOSIT,
        amount=Money.from_value("500"),
        occurred_at=now,
        note="bank deposit",
    )
    assert not accounting.append_journal(
        entry_id=original,
        entry_type=JournalType.DEPOSIT,
        amount=Money.from_value("500"),
        occurred_at=now,
        note="bank deposit",
    )
    with pytest.raises(AccountingError, match="different accounting"):
        accounting.append_journal(
            entry_id=original,
            entry_type=JournalType.DEPOSIT,
            amount=Money.from_value("501"),
            occurred_at=now,
            note="bank deposit",
        )
    assert accounting.append_journal(
        entry_id=reversed_entry,
        entry_type=JournalType.REVERSAL,
        amount=Money.from_value("-500"),
        occurred_at=now,
        reversal_of=original,
    )
    assert accounting.cash_balance() == Money.from_value(0)
    with pytest.raises(AccountingError, match="requires"):
        accounting.append_journal(
            entry_id=uuid4(),
            entry_type=JournalType.REVERSAL,
            amount=Money.from_value("-1"),
            occurred_at=now,
        )


def test_reconciliation_reports_broker_position_differences(tmp_path: Path) -> None:
    accounting = service(tmp_path)
    accounting.apply_fill(fill("buy", OrderSide.BUY, 10, "100"))
    assert (
        accounting.reconcile_positions({InstrumentKey("NSE:INFY"): (10, Money.from_value("100"))})
        == ()
    )
    differences = accounting.reconcile_positions(
        {InstrumentKey("NSE:INFY"): (9, Money.from_value("100"))}
    )
    assert len(differences) == 1
    assert differences[0].layer == "positions"


def test_accounting_inputs_fail_closed(tmp_path: Path) -> None:
    accounting = service(tmp_path)
    with pytest.raises(AccountingError, match="positive"):
        fill("bad", OrderSide.BUY, 0, "1")
    with pytest.raises(AccountingError, match="unknown"):
        accounting.value_position(
            InstrumentKey("NSE:MISSING"), Money.from_value("1"), datetime.now(UTC)
        )
    with pytest.raises(AccountingError, match="timezone-aware"):
        accounting.append_journal(
            entry_id=uuid4(),
            entry_type=JournalType.DEPOSIT,
            amount=Money.from_value("1"),
            occurred_at=datetime.now(),
        )
