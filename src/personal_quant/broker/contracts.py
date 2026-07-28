"""Broker-neutral models and the interface consumed by runtime components."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from personal_quant.domain.identifiers import BrokerOrderId, ClientOrderId, FillId, InstrumentKey
from personal_quant.domain.money import Money


class BrokerError(RuntimeError):
    """A safe broker failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class BrokerTimeout(BrokerError):
    """An ambiguous timeout for which callers must reconcile, never blind-retry."""


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BrokerProfile:
    user_id: str
    user_name: str
    broker: str
    exchanges: tuple[str, ...]
    products: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FundsSnapshot:
    available_cash: Money


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument: InstrumentKey
    quantity: int
    average_price: Money


@dataclass(frozen=True, slots=True)
class BrokerHolding:
    instrument: InstrumentKey
    quantity: int
    average_price: Money


@dataclass(frozen=True, slots=True)
class BrokerOrderRequest:
    client_order_id: ClientOrderId
    instrument: InstrumentKey
    side: OrderSide
    quantity: int
    limit_price: Money
    tag: str

    def __post_init__(self) -> None:
        _validate_instrument(self.instrument)
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.limit_price.amount <= 0:
            raise ValueError("limit price must be positive")
        if not self.tag.isascii() or not self.tag.isalnum() or len(self.tag) > 20:
            raise ValueError("order tag must be 1-20 alphanumeric characters")


@dataclass(frozen=True, slots=True)
class BrokerModifyRequest:
    broker_order_id: BrokerOrderId
    quantity: int | None = None
    limit_price: Money | None = None

    def __post_init__(self) -> None:
        if self.quantity is None and self.limit_price is None:
            raise ValueError("modify request must change quantity or limit price")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("modified quantity must be positive")
        if self.limit_price is not None and self.limit_price.amount <= 0:
            raise ValueError("modified limit price must be positive")


@dataclass(frozen=True, slots=True)
class BrokerCancelRequest:
    broker_order_id: BrokerOrderId


@dataclass(frozen=True, slots=True)
class BrokerOrderAck:
    broker_order_id: BrokerOrderId
    status: BrokerOrderStatus


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    broker_order_id: BrokerOrderId
    client_order_id: ClientOrderId | None
    instrument: InstrumentKey
    side: OrderSide
    quantity: int
    filled_quantity: int
    pending_quantity: int
    limit_price: Money
    average_price: Money
    status: BrokerOrderStatus
    placed_at: datetime
    status_message: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerTrade:
    fill_id: FillId
    broker_order_id: BrokerOrderId
    instrument: InstrumentKey
    side: OrderSide
    quantity: int
    price: Money
    filled_at: datetime


class Broker(Protocol):
    """The only broker surface runtime components may depend upon."""

    def get_profile(self) -> BrokerProfile: ...

    def get_funds(self) -> FundsSnapshot: ...

    def get_positions(self) -> Sequence[BrokerPosition]: ...

    def get_holdings(self) -> Sequence[BrokerHolding]: ...

    def get_orders(self) -> Sequence[BrokerOrder]: ...

    def get_trades(self) -> Sequence[BrokerTrade]: ...

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderAck: ...

    def modify_order(self, request: BrokerModifyRequest) -> BrokerOrderAck: ...

    def cancel_order(self, request: BrokerCancelRequest) -> BrokerOrderAck: ...


def split_instrument(instrument: InstrumentKey) -> tuple[str, str]:
    """Split the durable ``EXCHANGE:TRADINGSYMBOL`` key."""
    _validate_instrument(instrument)
    exchange, symbol = str(instrument).split(":", maxsplit=1)
    return exchange, symbol


def _validate_instrument(instrument: InstrumentKey) -> None:
    parts = str(instrument).split(":")
    if len(parts) != 2 or not all(parts) or parts[0] != "NSE":
        raise ValueError("instrument must use the durable NSE:TRADINGSYMBOL form")
