import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from personal_quant.broker.contracts import (
    BrokerCancelRequest,
    BrokerError,
    BrokerModifyRequest,
    BrokerOrderRequest,
    BrokerOrderStatus,
    OrderSide,
)
from personal_quant.broker.sandbox import (
    SANDBOX_ROOT,
    SandboxKiteAdapter,
    create_sandbox_client,
    patch_sandbox_routes,
)
from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import ClientOrderId, InstrumentKey
from personal_quant.domain.money import Money

FIXTURES = Path(__file__).parent / "fixtures" / "broker"


class FakeKiteClient:
    def __init__(self) -> None:
        self._routes = {
            "user.profile": "/user/profile",
            "market.instruments.all": "/instruments",
        }
        self._default_login_uri = f"{SANDBOX_ROOT}/connect/login"
        self.calls: list[tuple[str, dict[str, object]]] = []

    def login_url(self) -> str:
        return f"{SANDBOX_ROOT}/connect/login?api_key=fixture"

    def generate_session(self, request_token: str, api_secret: str) -> dict[str, Any]:
        return {"access_token": "fixture-access", "user_id": "SANDBOX01"}

    def set_access_token(self, access_token: str) -> None:
        self.calls.append(("set_access_token", {"access_token": access_token}))

    def profile(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads((FIXTURES / "sandbox_profile.json").read_text(encoding="utf-8")),
        )

    def margins(self, segment: str | None = None) -> dict[str, Any]:
        return {"net": 10000.25}

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "net": [
                {
                    "exchange": "NSE",
                    "tradingsymbol": "HDFCBANK",
                    "quantity": 2,
                    "average_price": 1649.5,
                }
            ]
        }

    def holdings(self) -> list[dict[str, Any]]:
        return self.positions()["net"]

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        return []

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime | str,
        to_date: datetime | str,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]:
        return []

    def orders(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            json.loads((FIXTURES / "sandbox_orders.json").read_text(encoding="utf-8")),
        )

    def trades(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            json.loads((FIXTURES / "sandbox_trades.json").read_text(encoding="utf-8")),
        )

    def place_order(self, **parameters: object) -> str:
        self.calls.append(("place_order", parameters))
        return "100000000000002"

    def modify_order(self, **parameters: object) -> str:
        self.calls.append(("modify_order", parameters))
        return str(parameters["order_id"])

    def cancel_order(self, **parameters: object) -> str:
        self.calls.append(("cancel_order", parameters))
        return str(parameters["order_id"])


def adapter() -> tuple[SandboxKiteAdapter, FakeKiteClient]:
    client = FakeKiteClient()
    clock = SimulatedClock(datetime(2026, 7, 28, 10, 0, tzinfo=UTC))
    return SandboxKiteAdapter(client, clock), client


def test_sandbox_route_patch_never_uses_production_root() -> None:
    client = patch_sandbox_routes(FakeKiteClient())

    assert SANDBOX_ROOT == "https://sandbox.kite.trade"
    assert client._routes["user.profile"] == "/oms/user/profile"
    assert client._routes["market.instruments.all"] == "/instruments"


def test_current_official_sdk_accepts_sandbox_configuration_without_network() -> None:
    client = create_sandbox_client("fixture-key")

    assert "sandbox.kite.trade" in client.login_url()
    assert client._routes["user.profile"].startswith("/oms/")


def test_sandbox_read_models_map_redacted_fixtures() -> None:
    sandbox, _ = adapter()

    assert sandbox.get_profile().user_id == "SANDBOX01"
    assert sandbox.get_funds().available_cash == Money.from_value("10000.25")
    assert sandbox.get_positions()[0].instrument == InstrumentKey("NSE:HDFCBANK")
    assert sandbox.get_holdings()[0].quantity == 2
    assert sandbox.get_orders()[0].status is BrokerOrderStatus.PARTIALLY_FILLED
    assert sandbox.get_orders()[0].placed_at.tzinfo is UTC
    assert sandbox.get_trades()[0].price == Money.from_value("1649.50")


def test_sandbox_order_calls_are_limit_cnc_regular_only() -> None:
    sandbox, client = adapter()
    request = BrokerOrderRequest(
        client_order_id=ClientOrderId(str(uuid4())),
        instrument=InstrumentKey("NSE:HDFCBANK"),
        side=OrderSide.BUY,
        quantity=1,
        limit_price=Money.from_value("1650.25"),
        tag="pqsandbox01",
    )
    ack = sandbox.place_order(request)
    sandbox.modify_order(
        BrokerModifyRequest(ack.broker_order_id, limit_price=Money.from_value("1651.00"))
    )
    cancelled = sandbox.cancel_order(BrokerCancelRequest(ack.broker_order_id))

    place = next(parameters for name, parameters in client.calls if name == "place_order")
    assert place["order_type"] == "LIMIT"
    assert place["product"] == "CNC"
    assert place["variety"] == "regular"
    assert place["exchange"] == "NSE"
    assert place["price"] == "1650.25"
    assert cancelled.status is BrokerOrderStatus.CANCELLED


def test_broker_source_contains_no_production_order_root() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "personal_quant" / "broker" / "sandbox.py"
    ).read_text(encoding="utf-8")

    assert "https://api.kite.trade" not in source
    assert '"MARKET"' not in source


def test_sandbox_sdk_failures_are_structured_and_redacted() -> None:
    sandbox, client = adapter()

    def fail() -> dict[str, Any]:
        raise RuntimeError("leaked-token-value")

    client.profile = fail  # type: ignore[method-assign]
    with pytest.raises(BrokerError) as error:
        sandbox.get_profile()
    assert error.value.code == "sandbox_profile_failed"
    assert "leaked-token-value" not in str(error.value)
