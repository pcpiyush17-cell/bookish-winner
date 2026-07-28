from decimal import Decimal
from uuid import uuid4

import pytest

from personal_quant.broker.contracts import (
    BrokerModifyRequest,
    BrokerOrderRequest,
    OrderSide,
    split_instrument,
)
from personal_quant.domain.identifiers import BrokerOrderId, ClientOrderId, InstrumentKey
from personal_quant.domain.money import Money


def order_request(**overrides: object) -> BrokerOrderRequest:
    values: dict[str, object] = {
        "client_order_id": ClientOrderId(str(uuid4())),
        "instrument": InstrumentKey("NSE:HDFCBANK"),
        "side": OrderSide.BUY,
        "quantity": 1,
        "limit_price": Money.from_value("1650.25"),
        "tag": "pqtest01",
    }
    values.update(overrides)
    return BrokerOrderRequest(**values)  # type: ignore[arg-type]


def test_order_request_is_limit_only_by_construction() -> None:
    request = order_request()

    assert request.limit_price.amount == Decimal("1650.25")
    assert not hasattr(request, "order_type")
    assert split_instrument(request.instrument) == ("NSE", "HDFCBANK")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("instrument", InstrumentKey("BSE:RELIANCE"), "NSE:TRADINGSYMBOL"),
        ("quantity", 0, "quantity"),
        ("limit_price", Money.from_value(0), "limit price"),
        ("tag", "not-valid!", "tag"),
        ("tag", "टैग", "tag"),
        ("tag", "a" * 21, "tag"),
    ],
)
def test_order_request_rejects_unsafe_values(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        order_request(**{field: value})


def test_modify_request_requires_a_positive_change() -> None:
    order_id = BrokerOrderId("order-1")
    with pytest.raises(ValueError, match="must change"):
        BrokerModifyRequest(order_id)
    with pytest.raises(ValueError, match="quantity"):
        BrokerModifyRequest(order_id, quantity=0)
    with pytest.raises(ValueError, match="limit price"):
        BrokerModifyRequest(order_id, limit_price=Money.from_value(0))
