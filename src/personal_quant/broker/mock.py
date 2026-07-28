"""Deterministic in-memory broker for unit, integration, and replay tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

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
    BrokerTimeout,
    BrokerTrade,
    FundsSnapshot,
    OrderSide,
)
from .rate_limit import BrokerRateLimiter


@dataclass(slots=True)
class MockBroker:
    """A deterministic broker whose fills occur only when a test explicitly asks."""

    clock: Clock
    opening_cash: Money = field(default_factory=lambda: Money.from_value("10000.00"))
    profile: BrokerProfile = field(
        default_factory=lambda: BrokerProfile(
            user_id="MOCKUSER",
            user_name="Mock User",
            broker="MOCK",
            exchanges=("NSE",),
            products=("CNC",),
        )
    )
    limiter: BrokerRateLimiter = field(init=False)
    _orders: dict[BrokerOrderId, BrokerOrder] = field(default_factory=dict, init=False)
    _client_orders: dict[ClientOrderId, BrokerOrderId] = field(default_factory=dict, init=False)
    _trades: list[BrokerTrade] = field(default_factory=list, init=False)
    _cash: Money = field(init=False)
    _positions: dict[InstrumentKey, tuple[int, Money]] = field(default_factory=dict, init=False)
    _next_order: int = field(default=1, init=False)
    _next_fill: int = field(default=1, init=False)
    _reject_reason: str | None = field(default=None, init=False)
    _timeout_after_submit: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._cash = self.opening_cash
        self.limiter = BrokerRateLimiter(self.clock)

    def get_profile(self) -> BrokerProfile:
        return self.profile

    def get_funds(self) -> FundsSnapshot:
        return FundsSnapshot(self._cash)

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        return tuple(
            BrokerPosition(instrument, quantity, average_price)
            for instrument, (quantity, average_price) in sorted(
                self._positions.items(), key=lambda item: str(item[0])
            )
            if quantity != 0
        )

    def get_holdings(self) -> tuple[BrokerHolding, ...]:
        return tuple(
            BrokerHolding(position.instrument, position.quantity, position.average_price)
            for position in self.get_positions()
            if position.quantity > 0
        )

    def get_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(self._orders.values())

    def get_trades(self) -> tuple[BrokerTrade, ...]:
        return tuple(self._trades)

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderAck:
        existing = self._client_orders.get(request.client_order_id)
        if existing is not None:
            order = self._orders[existing]
            return BrokerOrderAck(existing, order.status)
        self.limiter.acquire_new_order()
        order_id = BrokerOrderId(f"MOCK{self._next_order:08d}")
        self._next_order += 1
        status = BrokerOrderStatus.REJECTED if self._reject_reason else BrokerOrderStatus.OPEN
        order = BrokerOrder(
            broker_order_id=order_id,
            client_order_id=request.client_order_id,
            instrument=request.instrument,
            side=request.side,
            quantity=request.quantity,
            filled_quantity=0,
            pending_quantity=request.quantity if status is BrokerOrderStatus.OPEN else 0,
            limit_price=request.limit_price,
            average_price=Money.from_value(0),
            status=status,
            placed_at=self.clock.now(),
            status_message=self._reject_reason,
        )
        self._reject_reason = None
        self._orders[order_id] = order
        self._client_orders[request.client_order_id] = order_id
        if self._timeout_after_submit:
            self._timeout_after_submit = False
            raise BrokerTimeout(
                "order_response_unknown",
                "Order response was lost; reconcile the order book before any retry",
            )
        return BrokerOrderAck(order_id, status)

    def modify_order(self, request: BrokerModifyRequest) -> BrokerOrderAck:
        order = self._modifiable(request.broker_order_id)
        self.limiter.acquire_modification(request.broker_order_id)
        quantity = request.quantity if request.quantity is not None else order.quantity
        if quantity < order.filled_quantity:
            raise BrokerError("quantity_below_filled", "Quantity cannot be below filled quantity")
        price = request.limit_price or order.limit_price
        updated = replace(
            order,
            quantity=quantity,
            pending_quantity=quantity - order.filled_quantity,
            limit_price=price,
        )
        self._orders[request.broker_order_id] = updated
        return BrokerOrderAck(request.broker_order_id, updated.status)

    def cancel_order(self, request: BrokerCancelRequest) -> BrokerOrderAck:
        order = self._modifiable(request.broker_order_id)
        self.limiter.record_cancellation()
        updated = replace(
            order,
            pending_quantity=0,
            status=BrokerOrderStatus.CANCELLED,
        )
        self._orders[request.broker_order_id] = updated
        return BrokerOrderAck(request.broker_order_id, updated.status)

    def fill(self, order_id: BrokerOrderId, quantity: int, price: Money) -> BrokerTrade:
        """Apply one explicit fill to an open mock order."""
        order = self._modifiable(order_id)
        if quantity <= 0 or quantity > order.pending_quantity:
            raise BrokerError("invalid_fill_quantity", "Fill quantity exceeds pending quantity")
        held_quantity = self._positions.get(order.instrument, (0, Money.from_value(0)))[0]
        if order.side is OrderSide.SELL and quantity > held_quantity:
            raise BrokerError(
                "short_position_blocked", "Mock broker does not permit short positions"
            )
        total_filled = order.filled_quantity + quantity
        previous_value = order.average_price.amount * order.filled_quantity
        average = Money((previous_value + price.amount * quantity) / Decimal(total_filled))
        status = (
            BrokerOrderStatus.FILLED
            if total_filled == order.quantity
            else BrokerOrderStatus.PARTIALLY_FILLED
        )
        self._orders[order_id] = replace(
            order,
            filled_quantity=total_filled,
            pending_quantity=order.quantity - total_filled,
            average_price=average,
            status=status,
        )
        trade = BrokerTrade(
            fill_id=FillId(f"FILL{self._next_fill:08d}"),
            broker_order_id=order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=quantity,
            price=price,
            filled_at=self.clock.now(),
        )
        self._next_fill += 1
        self._trades.append(trade)
        self._apply_position(trade)
        return trade

    def reject_next_order(self, reason: str) -> None:
        self._reject_reason = reason

    def timeout_next_order_after_submission(self) -> None:
        self._timeout_after_submit = True

    def _modifiable(self, order_id: BrokerOrderId) -> BrokerOrder:
        try:
            order = self._orders[order_id]
        except KeyError as error:
            raise BrokerError("order_not_found", "Broker order was not found") from error
        if order.status not in {BrokerOrderStatus.OPEN, BrokerOrderStatus.PARTIALLY_FILLED}:
            raise BrokerError("order_not_open", "Only open orders can be changed")
        return order

    def _apply_position(self, trade: BrokerTrade) -> None:
        old_quantity, old_average = self._positions.get(trade.instrument, (0, Money.from_value(0)))
        direction = 1 if trade.side is OrderSide.BUY else -1
        new_quantity = old_quantity + direction * trade.quantity
        notional = trade.price.multiply(trade.quantity)
        self._cash = self._cash - notional if direction == 1 else self._cash + notional
        if direction == 1 and new_quantity > 0:
            old_value = old_average.amount * old_quantity
            new_average = Money((old_value + notional.amount) / Decimal(new_quantity))
        else:
            new_average = old_average if new_quantity else Money.from_value(0)
        self._positions[trade.instrument] = (new_quantity, new_average)
