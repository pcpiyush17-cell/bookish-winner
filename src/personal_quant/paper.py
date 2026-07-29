"""Deterministic next-bar limit execution layered over the in-memory mock broker."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from personal_quant.broker.contracts import (
    BrokerCancelRequest,
    BrokerHolding,
    BrokerModifyRequest,
    BrokerOrder,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    FundsSnapshot,
    OrderSide,
)
from personal_quant.broker.mock import MockBroker
from personal_quant.clocks import Clock
from personal_quant.domain.identifiers import BrokerOrderId, InstrumentKey
from personal_quant.domain.money import Money


@dataclass(frozen=True, slots=True)
class MarketBar:
    instrument: InstrumentKey
    timestamp: datetime
    open: Money
    high: Money
    low: Money
    close: Money
    volume: int

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("paper bar timestamp must be timezone-aware")
        if (
            self.volume < 0
            or min(self.open.amount, self.high.amount, self.low.amount, self.close.amount) <= 0
        ):
            raise ValueError("paper bar prices must be positive and volume non-negative")
        if self.low.amount > min(self.open.amount, self.close.amount) or self.high.amount < max(
            self.open.amount, self.close.amount
        ):
            raise ValueError("paper bar OHLC bounds are invalid")


@dataclass(slots=True)
class PaperBroker:
    clock: Clock
    max_participation: Decimal = Decimal("0.10")
    opening_cash: Money = field(default_factory=lambda: Money.from_value("10000.00"))
    broker: MockBroker = field(init=False)

    def __post_init__(self) -> None:
        if not Decimal(0) < self.max_participation <= Decimal(1):
            raise ValueError("paper participation must be within (0, 1]")
        self.broker = MockBroker(self.clock, self.opening_cash)

    def process_bar(self, bar: MarketBar) -> tuple[BrokerTrade, ...]:
        fills: list[BrokerTrade] = []
        available = int(
            (Decimal(bar.volume) * self.max_participation).to_integral_value(rounding=ROUND_FLOOR)
        )
        for order in self.get_orders():
            if order.instrument != bar.instrument or order.pending_quantity <= 0:
                continue
            if bar.timestamp <= order.placed_at or available <= 0:
                continue
            touched = (
                order.side is OrderSide.BUY and bar.low.amount <= order.limit_price.amount
            ) or (order.side is OrderSide.SELL and bar.high.amount >= order.limit_price.amount)
            if not touched:
                continue
            quantity = min(order.pending_quantity, available)
            price = (
                Money(min(bar.open.amount, order.limit_price.amount))
                if order.side is OrderSide.BUY
                else Money(max(bar.open.amount, order.limit_price.amount))
            )
            fills.append(self.broker.fill(order.broker_order_id, quantity, price))
            available -= quantity
        return tuple(fills)

    def restore_portfolio(
        self,
        cash: Money,
        positions: Mapping[InstrumentKey, tuple[int, Money]],
    ) -> None:
        self.broker.restore_portfolio(cash, positions)

    def get_profile(self) -> BrokerProfile:
        return self.broker.get_profile()

    def get_funds(self) -> FundsSnapshot:
        return self.broker.get_funds()

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        return self.broker.get_positions()

    def get_holdings(self) -> tuple[BrokerHolding, ...]:
        return self.broker.get_holdings()

    def get_orders(self) -> tuple[BrokerOrder, ...]:
        return self.broker.get_orders()

    def get_trades(self) -> tuple[BrokerTrade, ...]:
        return self.broker.get_trades()

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        return self.broker.place_order(request)

    def modify_order(self, request: BrokerModifyRequest) -> BrokerOrderAck:
        return self.broker.modify_order(request)

    def cancel_order(self, request: BrokerCancelRequest) -> BrokerOrderAck:
        return self.broker.cancel_order(request)

    def fill(self, order_id: BrokerOrderId, quantity: int, price: Money) -> BrokerTrade:
        return self.broker.fill(order_id, quantity, price)
