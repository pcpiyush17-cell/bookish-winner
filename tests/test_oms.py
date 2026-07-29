import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from personal_quant.accounting import PortfolioAccounting
from personal_quant.broker.contracts import OrderSide
from personal_quant.broker.mock import MockBroker
from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import ClientOrderId, InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.oms import (
    OmsError,
    OmsIntent,
    OrderManagementSystem,
    OrderState,
    StoredOrder,
)
from personal_quant.paper import MarketBar, PaperBroker
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

NOW = datetime(2026, 7, 29, 5, tzinfo=UTC)
INSTRUMENT = InstrumentKey("NSE:INFY")


def setup(
    tmp_path: Path, *, broker: MockBroker | PaperBroker | None = None
) -> tuple[
    Database,
    SimulatedClock,
    MockBroker | PaperBroker,
    OrderManagementSystem,
]:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    clock = SimulatedClock(NOW)
    selected = broker or MockBroker(clock)
    oms = OrderManagementSystem(database, selected, clock, PortfolioAccounting(database))
    return database, clock, selected, oms


def approved_intent(
    database: Database,
    *,
    key: str | None = None,
    quantity: int = 10,
    approved: int | None = None,
    decision: str = "approved",
) -> tuple[OmsIntent, UUID]:
    risk_intent_id = uuid4()
    decision_id = uuid4()
    idempotency_key = key or uuid4().hex
    approved_quantity = quantity if approved is None else approved
    snapshot = {
        "intent": {
            "instrument_key": str(INSTRUMENT),
            "side": OrderSide.BUY.value,
            "quantity": quantity,
            "limit_price": {"amount": "100.00", "currency": "INR"},
        }
    }
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO risk_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(decision_id),
                str(risk_intent_id),
                idempotency_key,
                decision,
                quantity,
                approved_quantity,
                "[]",
                "0" * 64,
                json.dumps(snapshot),
                NOW.isoformat(),
            ),
        )
    return (
        OmsIntent(
            uuid4(),
            risk_intent_id,
            idempotency_key,
            INSTRUMENT,
            OrderSide.BUY,
            quantity,
            Money.from_value("100"),
        ),
        decision_id,
    )


def register_approved(
    database: Database,
    oms: OrderManagementSystem,
    *,
    key: str | None = None,
    quantity: int = 10,
) -> tuple[OmsIntent, StoredOrder]:
    intent, decision_id = approved_intent(database, key=key, quantity=quantity)
    return intent, oms.register(intent, decision_id)


def test_full_and_partial_fill_lifecycle_is_deduplicated_and_restart_safe(
    tmp_path: Path,
) -> None:
    database, clock, broker, oms = setup(tmp_path)
    intent, order = register_approved(database, oms)
    assert order.state is OrderState.RISK_APPROVED
    order = oms.submit(intent.order_id)
    assert order.state is OrderState.OPEN
    assert order.broker_order_id is not None

    broker.fill(order.broker_order_id, 4, Money.from_value("99.50"))
    partial = oms.sync(order.order_id)
    assert (partial.state, partial.filled_quantity) == (OrderState.PARTIALLY_FILLED, 4)
    assert PortfolioAccounting(database).position(INSTRUMENT).quantity == 4  # type: ignore[union-attr]

    broker.fill(order.broker_order_id, 6, Money.from_value("100.50"))
    filled = oms.sync(order.order_id)
    assert (filled.state, filled.filled_quantity) == (OrderState.FILLED, 10)
    assert filled.average_price == Money.from_value("100.10")
    assert oms.sync(order.order_id).filled_quantity == 10
    assert PortfolioAccounting(database).position(INSTRUMENT).quantity == 10  # type: ignore[union-attr]
    assert (
        OrderManagementSystem(database, broker, clock).get(order.order_id).state
        is OrderState.FILLED
    )


def test_partial_fill_can_be_cancelled_and_preserves_accounting(tmp_path: Path) -> None:
    database, _, broker, oms = setup(tmp_path)
    _, order = register_approved(database, oms)
    order = oms.submit(order.order_id)
    assert order.broker_order_id is not None
    broker.fill(order.broker_order_id, 3, Money.from_value("100"))
    assert oms.sync(order.order_id).state is OrderState.PARTIALLY_FILLED
    cancelled = oms.cancel(order.order_id)
    assert (cancelled.state, cancelled.filled_quantity) == (OrderState.CANCELLED, 3)


def test_rejection_and_risk_rejection_are_terminal(tmp_path: Path) -> None:
    database, _, broker, oms = setup(tmp_path)
    assert isinstance(broker, MockBroker)
    broker.reject_next_order("exchange rejected")
    _, order = register_approved(database, oms)
    assert oms.submit(order.order_id).state is OrderState.REJECTED

    intent, decision_id = approved_intent(
        database, approved=0, decision="rejected", key="risk-rejected"
    )
    rejected = oms.register(intent, decision_id)
    assert rejected.state is OrderState.RISK_REJECTED
    with pytest.raises(OmsError) as error:
        oms.submit(rejected.order_id)
    assert error.value.code == "submission_not_allowed"


def test_ambiguous_timeout_never_blindly_retries_and_blocks_instrument(tmp_path: Path) -> None:
    database, _, broker, oms = setup(tmp_path)
    assert isinstance(broker, MockBroker)
    broker.timeout_next_order_after_submission()
    _, order = register_approved(database, oms)
    unknown = oms.submit(order.order_id)
    assert unknown.state is OrderState.RECONCILIATION_REQUIRED
    assert len(broker.get_orders()) == 1
    with pytest.raises(OmsError):
        oms.submit(order.order_id)
    assert len(broker.get_orders()) == 1

    second_intent, second_decision = approved_intent(database, key="blocked-instrument")
    with pytest.raises(OmsError) as error:
        oms.register(second_intent, second_decision)
    assert error.value.code == "instrument_blocked"

    reconciled = oms.reconcile_unknown(order.order_id)
    assert reconciled.state is OrderState.OPEN
    assert len(broker.get_orders()) == 1


def test_unknown_order_reconciliation_accepts_zerodha_compact_tag(tmp_path: Path) -> None:
    database, _, broker, oms = setup(tmp_path)
    assert isinstance(broker, MockBroker)
    broker.timeout_next_order_after_submission()
    _, order = register_approved(database, oms)
    unknown = oms.submit(order.order_id)
    broker_order = broker.get_orders()[0]
    broker._orders[broker_order.broker_order_id] = replace(
        broker_order, client_order_id=ClientOrderId(unknown.broker_tag)
    )

    assert oms.reconcile_unknown(order.order_id).state is OrderState.OPEN


def test_idempotency_risk_binding_and_modification_limits(tmp_path: Path) -> None:
    database, clock, _, oms = setup(tmp_path)
    intent, order = register_approved(database, oms)
    assert oms.register(intent, order.risk_decision_id).order_id == order.order_id
    conflict = OmsIntent(
        uuid4(),
        intent.risk_intent_id,
        intent.idempotency_key,
        intent.instrument,
        intent.side,
        9,
        intent.limit_price,
    )
    with pytest.raises(OmsError) as error:
        oms.register(conflict, order.risk_decision_id)
    assert error.value.code == "intent_conflict"

    submitted = oms.submit(order.order_id)
    clock.advance(timedelta(seconds=6))
    changed = oms.modify(submitted.order_id, quantity=9)
    assert (changed.approved_quantity, changed.modification_count) == (9, 1)
    clock.advance(timedelta(seconds=6))
    changed = oms.modify(submitted.order_id, limit_price=Money.from_value("99"))
    assert changed.modification_count == 2
    clock.advance(timedelta(seconds=6))
    with pytest.raises(OmsError) as error:
        oms.modify(submitted.order_id, limit_price=Money.from_value("98"))
    assert error.value.code == "modification_limit"


def test_paper_broker_uses_next_bar_touch_and_participation_cap(tmp_path: Path) -> None:
    database = Database(tmp_path / "paper.sqlite")
    MigrationRunner(database).apply_all()
    clock = SimulatedClock(NOW)
    paper = PaperBroker(clock)
    oms = OrderManagementSystem(database, paper, clock)
    _, order = register_approved(database, oms, quantity=10)
    opened = oms.submit(order.order_id)
    assert opened.broker_order_id is not None

    same_bar = MarketBar(
        INSTRUMENT,
        NOW,
        Money.from_value("101"),
        Money.from_value("102"),
        Money.from_value("99"),
        Money.from_value("100"),
        50,
    )
    assert paper.process_bar(same_bar) == ()
    clock.advance(timedelta(minutes=1))
    fills = paper.process_bar(
        MarketBar(
            INSTRUMENT,
            clock.now(),
            Money.from_value("99"),
            Money.from_value("101"),
            Money.from_value("98"),
            Money.from_value("100"),
            50,
        )
    )
    assert len(fills) == 1
    assert (fills[0].quantity, fills[0].price) == (5, Money.from_value("99"))
    assert oms.sync(order.order_id).state is OrderState.PARTIALLY_FILLED
