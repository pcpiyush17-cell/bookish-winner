from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from personal_quant.broker.contracts import (
    BrokerCancelRequest,
    BrokerError,
    BrokerModifyRequest,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerTimeout,
    OrderSide,
)
from personal_quant.broker.mock import MockBroker
from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import ClientOrderId, InstrumentKey
from personal_quant.domain.money import Money


def request(
    *, side: OrderSide = OrderSide.BUY, quantity: int = 3, tag: str = "pqmock01"
) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id=ClientOrderId(str(uuid4())),
        instrument=InstrumentKey("NSE:HDFCBANK"),
        side=side,
        quantity=quantity,
        limit_price=Money.from_value("100.00"),
        tag=tag,
    )


def broker() -> tuple[MockBroker, SimulatedClock]:
    clock = SimulatedClock(datetime(2026, 7, 28, 10, 0, tzinfo=UTC))
    return MockBroker(clock), clock


def test_complete_mock_order_lifecycle_with_partial_fill() -> None:
    mock, clock = broker()
    submitted = request()
    ack = mock.place_order(submitted)
    assert ack.status is BrokerOrderStatus.OPEN

    first_fill = mock.fill(ack.broker_order_id, 1, Money.from_value("99.00"))
    assert first_fill.quantity == 1
    assert mock.get_orders()[0].status is BrokerOrderStatus.PARTIALLY_FILLED

    clock.advance(timedelta(seconds=1))
    mock.modify_order(
        BrokerModifyRequest(ack.broker_order_id, limit_price=Money.from_value("101.00"))
    )
    mock.fill(ack.broker_order_id, 2, Money.from_value("100.00"))

    order = mock.get_orders()[0]
    assert order.status is BrokerOrderStatus.FILLED
    assert order.pending_quantity == 0
    assert len(mock.get_trades()) == 2
    assert mock.get_positions()[0].quantity == 3
    assert mock.get_holdings()[0].quantity == 3
    assert mock.get_funds().available_cash == Money.from_value("9701.00")

    with pytest.raises(BrokerError) as closed_error:
        mock.cancel_order(BrokerCancelRequest(ack.broker_order_id))
    assert closed_error.value.code == "order_not_open"


def test_cancel_preserves_partial_fill() -> None:
    mock, _ = broker()
    ack = mock.place_order(request())
    mock.fill(ack.broker_order_id, 1, Money.from_value("100"))

    cancelled = mock.cancel_order(BrokerCancelRequest(ack.broker_order_id))

    assert cancelled.status is BrokerOrderStatus.CANCELLED
    assert mock.get_orders()[0].filled_quantity == 1
    assert mock.get_positions()[0].quantity == 1


def test_rejection_and_unknown_response_are_deterministic() -> None:
    mock, _ = broker()
    mock.reject_next_order("fixture rejection")
    rejected = mock.place_order(request())
    assert rejected.status is BrokerOrderStatus.REJECTED
    assert mock.get_orders()[0].status_message == "fixture rejection"

    mock.timeout_next_order_after_submission()
    timed_out_request = request(tag="pqmock02")
    with pytest.raises(BrokerTimeout) as timeout:
        mock.place_order(timed_out_request)
    assert timeout.value.code == "order_response_unknown"
    assert len(mock.get_orders()) == 2

    reconciled = mock.place_order(timed_out_request)
    assert reconciled.broker_order_id == mock.get_orders()[1].broker_order_id


def test_modification_limits_and_invalid_fills() -> None:
    mock, _ = broker()
    ack = mock.place_order(request())
    mock.modify_order(BrokerModifyRequest(ack.broker_order_id, quantity=4))
    mock.modify_order(BrokerModifyRequest(ack.broker_order_id, quantity=5))
    with pytest.raises(BrokerError) as limit_error:
        mock.modify_order(BrokerModifyRequest(ack.broker_order_id, quantity=6))
    assert limit_error.value.code == "modification_limit"

    with pytest.raises(BrokerError) as fill_error:
        mock.fill(ack.broker_order_id, 6, Money.from_value("100"))
    assert fill_error.value.code == "invalid_fill_quantity"


def test_missing_order_is_structured_error() -> None:
    mock, _ = broker()
    with pytest.raises(BrokerError) as error:
        mock.cancel_order(BrokerCancelRequest("missing"))  # type: ignore[arg-type]
    assert error.value.code == "order_not_found"


def test_sell_fill_updates_cash_but_short_fill_is_blocked() -> None:
    mock, _ = broker()
    buy = mock.place_order(request(quantity=1, tag="pqbuy01"))
    mock.fill(buy.broker_order_id, 1, Money.from_value("100"))
    sell = mock.place_order(request(side=OrderSide.SELL, quantity=1, tag="pqsell01"))
    mock.fill(sell.broker_order_id, 1, Money.from_value("110"))

    assert mock.get_positions() == ()
    assert mock.get_funds().available_cash == Money.from_value("10010")

    short_mock, _ = broker()
    short = short_mock.place_order(request(side=OrderSide.SELL, quantity=1))
    with pytest.raises(BrokerError) as error:
        short_mock.fill(short.broker_order_id, 1, Money.from_value("100"))
    assert error.value.code == "short_position_blocked"
