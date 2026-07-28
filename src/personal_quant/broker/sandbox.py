"""Kite Connect sandbox-only adapter using the official Python SDK."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, cast
from zoneinfo import ZoneInfo

from personal_quant.clocks import Clock
from personal_quant.domain.identifiers import BrokerOrderId, ClientOrderId, FillId, InstrumentKey
from personal_quant.domain.money import Money

from .contracts import (
    BrokerCancelRequest,
    BrokerError,
    BrokerHolding,
    BrokerModifyRequest,
    BrokerOrder,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    FundsSnapshot,
    OrderSide,
    split_instrument,
)
from .rate_limit import BrokerRateLimiter

SANDBOX_ROOT = "https://sandbox.kite.trade"
_INDIA = ZoneInfo("Asia/Kolkata")
_INSTRUMENT_ROUTES = {"market.instruments.all", "market.instruments"}
_Result = TypeVar("_Result")


class KiteClient(Protocol):
    _routes: dict[str, str]
    _default_login_uri: str

    def login_url(self) -> str: ...

    def generate_session(self, request_token: str, api_secret: str) -> dict[str, Any]: ...

    def set_access_token(self, access_token: str) -> None: ...

    def profile(self) -> dict[str, Any]: ...

    def margins(self, segment: str | None = None) -> dict[str, Any]: ...

    def positions(self) -> dict[str, list[dict[str, Any]]]: ...

    def holdings(self) -> list[dict[str, Any]]: ...

    def orders(self) -> list[dict[str, Any]]: ...

    def trades(self) -> list[dict[str, Any]]: ...

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]: ...

    def place_order(self, **parameters: object) -> str: ...

    def modify_order(self, **parameters: object) -> str: ...

    def cancel_order(self, **parameters: object) -> str: ...


def create_sandbox_client(api_key: str) -> KiteClient:
    """Create an SDK client that cannot address the production API root."""
    from kiteconnect import KiteConnect  # type: ignore[import-untyped]

    client = KiteConnect(api_key=api_key, root=SANDBOX_ROOT)
    sandbox_client = cast(KiteClient, client)
    sandbox_client._default_login_uri = f"{SANDBOX_ROOT}/connect/login"
    return patch_sandbox_routes(sandbox_client)


def patch_sandbox_routes(client: KiteClient) -> KiteClient:
    """Apply the `/oms` route prefix required by the official sandbox."""
    client._routes = {
        key: value if key in _INSTRUMENT_ROUTES else f"/oms{value}"
        for key, value in client._routes.items()
    }
    return client


class SandboxKiteAdapter:
    """Map the sandbox SDK to broker-neutral, LIMIT-only domain contracts."""

    def __init__(self, client: KiteClient, clock: Clock) -> None:
        self._client = client
        self._limiter = BrokerRateLimiter(clock)

    def get_profile(self) -> BrokerProfile:
        raw = self._safe_call("profile", self._client.profile)
        return BrokerProfile(
            user_id=str(raw["user_id"]),
            user_name=str(raw["user_name"]),
            broker=str(raw["broker"]),
            exchanges=tuple(str(value) for value in raw["exchanges"]),
            products=tuple(str(value) for value in raw["products"]),
        )

    def get_funds(self) -> FundsSnapshot:
        raw = self._safe_call("funds", lambda: self._client.margins("equity"))
        return FundsSnapshot(Money.from_value(str(raw["net"])))

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        raw = self._safe_call("positions", self._client.positions)
        return tuple(_position(item) for item in raw.get("net", []))

    def get_holdings(self) -> tuple[BrokerHolding, ...]:
        raw = self._safe_call("holdings", self._client.holdings)
        return tuple(_holding(item) for item in raw)

    def get_orders(self) -> tuple[BrokerOrder, ...]:
        raw = self._safe_call("orders", self._client.orders)
        return tuple(_order(item) for item in raw)

    def get_trades(self) -> tuple[BrokerTrade, ...]:
        raw = self._safe_call("trades", self._client.trades)
        return tuple(_trade(item) for item in raw)

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        self._limiter.acquire_new_order()
        exchange, symbol = split_instrument(request.instrument)
        order_id = self._safe_call(
            "place_order",
            lambda: self._client.place_order(
                variety="regular",
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=request.side.value,
                quantity=request.quantity,
                product="CNC",
                order_type="LIMIT",
                price=str(request.limit_price.amount),
                validity="DAY",
                tag=request.tag,
            ),
        )
        return BrokerOrderAck(BrokerOrderId(str(order_id)), BrokerOrderStatus.OPEN)

    def modify_order(self, request: BrokerModifyRequest) -> BrokerOrderAck:
        self._limiter.acquire_modification(request.broker_order_id)
        parameters: dict[str, object] = {
            "variety": "regular",
            "order_id": str(request.broker_order_id),
            "order_type": "LIMIT",
        }
        if request.quantity is not None:
            parameters["quantity"] = request.quantity
        if request.limit_price is not None:
            parameters["price"] = str(request.limit_price.amount)
        order_id = self._safe_call("modify_order", lambda: self._client.modify_order(**parameters))
        return BrokerOrderAck(BrokerOrderId(str(order_id)), BrokerOrderStatus.OPEN)

    def cancel_order(self, request: BrokerCancelRequest) -> BrokerOrderAck:
        self._limiter.record_cancellation()
        order_id = self._safe_call(
            "cancel_order",
            lambda: self._client.cancel_order(
                variety="regular", order_id=str(request.broker_order_id)
            ),
        )
        return BrokerOrderAck(BrokerOrderId(str(order_id)), BrokerOrderStatus.CANCELLED)

    @staticmethod
    def _safe_call(operation: str, call: Callable[[], _Result]) -> _Result:
        try:
            return call()
        except Exception:
            raise BrokerError(
                f"sandbox_{operation}_failed",
                f"Sandbox {operation} failed; sensitive details were redacted",
            ) from None


def _position(raw: dict[str, Any]) -> BrokerPosition:
    return BrokerPosition(
        instrument=_instrument(raw),
        quantity=int(raw["quantity"]),
        average_price=Money.from_value(str(raw["average_price"])),
    )


def _holding(raw: dict[str, Any]) -> BrokerHolding:
    return BrokerHolding(
        instrument=_instrument(raw),
        quantity=int(raw["quantity"]),
        average_price=Money.from_value(str(raw["average_price"])),
    )


def _order(raw: dict[str, Any]) -> BrokerOrder:
    filled_quantity = int(raw["filled_quantity"])
    pending_quantity = int(raw["pending_quantity"])
    status = _status(str(raw["status"]), filled_quantity, pending_quantity)
    tag = raw.get("tag")
    return BrokerOrder(
        broker_order_id=BrokerOrderId(str(raw["order_id"])),
        client_order_id=ClientOrderId(str(tag)) if tag else None,
        instrument=_instrument(raw),
        side=OrderSide(str(raw["transaction_type"])),
        quantity=int(raw["quantity"]),
        filled_quantity=filled_quantity,
        pending_quantity=pending_quantity,
        limit_price=Money.from_value(str(raw["price"])),
        average_price=Money.from_value(str(raw["average_price"])),
        status=status,
        placed_at=_timestamp(raw["order_timestamp"]),
        status_message=str(raw["status_message"]) if raw.get("status_message") else None,
    )


def _trade(raw: dict[str, Any]) -> BrokerTrade:
    return BrokerTrade(
        fill_id=FillId(str(raw["trade_id"])),
        broker_order_id=BrokerOrderId(str(raw["order_id"])),
        instrument=_instrument(raw),
        side=OrderSide(str(raw["transaction_type"])),
        quantity=int(raw["quantity"]),
        price=Money.from_value(str(raw["average_price"])),
        filled_at=_timestamp(raw["fill_timestamp"]),
    )


def _instrument(raw: dict[str, Any]) -> InstrumentKey:
    return InstrumentKey(f"{raw['exchange']}:{raw['tradingsymbol']}")


def _status(value: str, filled_quantity: int, pending_quantity: int) -> BrokerOrderStatus:
    if value == "COMPLETE":
        return BrokerOrderStatus.FILLED
    if value in {"CANCELLED", "REJECTED"}:
        return BrokerOrderStatus(value)
    partial = value == "PARTIALLY_FILLED" or (filled_quantity > 0 and pending_quantity > 0)
    if partial:
        return BrokerOrderStatus.PARTIALLY_FILLED
    open_statuses = {
        "OPEN",
        "PUT ORDER REQ RECEIVED",
        "VALIDATION PENDING",
        "OPEN PENDING",
        "MODIFY VALIDATION PENDING",
        "MODIFY PENDING",
        "TRIGGER PENDING",
        "CANCEL PENDING",
    }
    return BrokerOrderStatus.OPEN if value in open_statuses else BrokerOrderStatus.UNKNOWN


def _timestamp(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_INDIA)
    return parsed.astimezone(UTC)
