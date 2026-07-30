"""Persistent order state machine with idempotent, risk-gated broker submission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from personal_quant.accounting import Fill as AccountingFill
from personal_quant.accounting import FillCost, PortfolioAccounting
from personal_quant.broker.contracts import (
    Broker,
    BrokerCancelRequest,
    BrokerError,
    BrokerModifyRequest,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerTimeout,
    BrokerTrade,
    OrderSide,
)
from personal_quant.clocks import Clock
from personal_quant.domain.identifiers import (
    BrokerOrderId,
    ClientOrderId,
    FillId,
    InstrumentKey,
)
from personal_quant.domain.money import Money
from personal_quant.storage.database import Database


class OmsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OrderState(StrEnum):
    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.RISK_APPROVED, OrderState.RISK_REJECTED}),
    OrderState.RISK_APPROVED: frozenset({OrderState.SUBMISSION_PENDING}),
    OrderState.SUBMISSION_PENDING: frozenset(
        {OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.REJECTED}
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }
    ),
    OrderState.OPEN: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.REJECTED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING}
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {OrderState.CANCELLED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.UNKNOWN}
    ),
    OrderState.UNKNOWN: frozenset({OrderState.RECONCILIATION_REQUIRED}),
    OrderState.RECONCILIATION_REQUIRED: frozenset(
        {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.RISK_APPROVED,
        }
    ),
    OrderState.RISK_REJECTED: frozenset(),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class OmsIntent:
    order_id: UUID
    risk_intent_id: UUID
    idempotency_key: str
    instrument: InstrumentKey
    side: OrderSide
    requested_quantity: int
    limit_price: Money


@dataclass(frozen=True, slots=True)
class StoredOrder:
    order_id: UUID
    idempotency_key: str
    risk_decision_id: UUID
    instrument: InstrumentKey
    side: OrderSide
    requested_quantity: int
    approved_quantity: int
    limit_price: Money
    state: OrderState
    client_order_id: ClientOrderId
    broker_tag: str
    broker_order_id: BrokerOrderId | None
    filled_quantity: int
    average_price: Money
    modification_count: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OrderManagementSystem:
    database: Database
    broker: Broker
    clock: Clock
    accounting: PortfolioAccounting | None = None
    fill_cost_estimator: (
        Callable[[AccountingFill, PortfolioAccounting], tuple[FillCost, ...]] | None
    ) = None
    cost_debiter: Callable[[Money], None] | None = None
    minimum_modify_wait: timedelta = timedelta(seconds=5)

    def register(self, intent: OmsIntent, risk_decision_id: UUID) -> StoredOrder:
        if intent.requested_quantity <= 0 or intent.limit_price.amount <= 0:
            raise OmsError("intent_invalid", "Order intent quantity and price must be positive")
        now = _aware_iso(self.clock.now())
        with self.database.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT order_id FROM oms_orders WHERE idempotency_key=?",
                (intent.idempotency_key,),
            ).fetchone()
            if existing is not None:
                stored = self._get_with_connection(connection, UUID(existing["order_id"]))
                if (
                    stored.instrument != intent.instrument
                    or stored.side is not intent.side
                    or stored.requested_quantity != intent.requested_quantity
                    or stored.limit_price != intent.limit_price
                ):
                    raise OmsError("intent_conflict", "Idempotency key has different economics")
                return stored
            blocked = connection.execute(
                """
                SELECT 1 FROM oms_orders
                WHERE instrument_key=? AND state IN ('UNKNOWN', 'RECONCILIATION_REQUIRED')
                LIMIT 1
                """,
                (str(intent.instrument),),
            ).fetchone()
            if blocked is not None:
                raise OmsError(
                    "instrument_blocked",
                    "Instrument has an unresolved ambiguous broker outcome",
                )
            risk = connection.execute(
                """
                SELECT intent_id, idempotency_key, decision, requested_quantity,
                       approved_quantity, snapshot_json
                FROM risk_decisions WHERE decision_id=?
                """,
                (str(risk_decision_id),),
            ).fetchone()
            if risk is None:
                raise OmsError("risk_decision_missing", "Persisted risk decision is required")
            self._verify_risk_economics(intent, risk)
            approved = int(risk["approved_quantity"])
            state = (
                OrderState.RISK_REJECTED
                if risk["decision"] == "rejected"
                else OrderState.RISK_APPROVED
            )
            client_id, tag = _client_identity(intent)
            connection.execute(
                """
                INSERT INTO oms_orders(
                    order_id, idempotency_key, risk_decision_id, instrument_key, side,
                    requested_quantity, approved_quantity, limit_price_paise, state,
                    client_order_id, broker_tag, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(intent.order_id),
                    intent.idempotency_key,
                    str(risk_decision_id),
                    str(intent.instrument),
                    intent.side.value,
                    intent.requested_quantity,
                    approved,
                    _paise(intent.limit_price),
                    state.value,
                    str(client_id),
                    tag,
                    now,
                    now,
                ),
            )
            self._event(connection, intent.order_id, OrderState.CREATED, state, "risk_decision", {})
            return self._get_with_connection(connection, intent.order_id)

    @staticmethod
    def _verify_risk_economics(intent: OmsIntent, risk: sqlite3.Row) -> None:
        snapshot = json.loads(str(risk["snapshot_json"]))["intent"]
        matches = (
            str(risk["intent_id"]) == str(intent.risk_intent_id)
            and risk["idempotency_key"] == intent.idempotency_key
            and int(risk["requested_quantity"]) == intent.requested_quantity
            and snapshot["instrument_key"] == str(intent.instrument)
            and snapshot["side"] == intent.side.value
            and snapshot["limit_price"]["amount"] == str(intent.limit_price.amount)
        )
        if not matches:
            raise OmsError(
                "risk_economics_mismatch",
                "Risk decision does not describe this exact order intent",
            )

    def submit(self, order_id: UUID) -> StoredOrder:
        order = self.get(order_id)
        if order.state is not OrderState.RISK_APPROVED:
            raise OmsError("submission_not_allowed", "Only risk-approved orders can be submitted")
        self._transition(order_id, OrderState.SUBMISSION_PENDING, "submission_started")
        request = BrokerOrderRequest(
            order.client_order_id,
            order.instrument,
            order.side,
            order.approved_quantity,
            order.limit_price,
            order.broker_tag,
        )
        try:
            acknowledgement = self.broker.place_order(request)
        except BrokerTimeout as error:
            self._transition(order_id, OrderState.UNKNOWN, error.code, error.code)
            self._transition(order_id, OrderState.RECONCILIATION_REQUIRED, "unknown_outcome")
            self._incident(order_id, "unknown_submission", "Broker response was ambiguous")
            return self.get(order_id)
        except BrokerError as error:
            self._transition(order_id, OrderState.REJECTED, error.code, error.code)
            return self.get(order_id)
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE oms_orders SET broker_order_id=? WHERE order_id=?",
                (str(acknowledgement.broker_order_id), str(order_id)),
            )
        self._transition(order_id, OrderState.SUBMITTED, "broker_response")
        target = _state_from_broker(acknowledgement.status)
        self._transition(order_id, target, "broker_ack")
        return self.get(order_id)

    def sync(self, order_id: UUID) -> StoredOrder:
        order = self.get(order_id)
        if order.broker_order_id is None:
            return order
        broker_order = next(
            (
                item
                for item in self.broker.get_orders()
                if item.broker_order_id == order.broker_order_id
            ),
            None,
        )
        if broker_order is None:
            raise OmsError("broker_order_missing", "Broker order is missing during synchronization")
        for trade in self.broker.get_trades():
            if trade.broker_order_id == order.broker_order_id:
                self._record_fill(order_id, trade)
        refreshed = self.get(order_id)
        target = _state_from_broker(broker_order.status)
        if target != refreshed.state and target in TRANSITIONS[refreshed.state]:
            self._transition(order_id, target, "broker_sync")
        return self.get(order_id)

    def cancel(self, order_id: UUID) -> StoredOrder:
        order = self.sync(order_id)
        if order.broker_order_id is None or order.state not in {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
        }:
            raise OmsError("cancel_not_allowed", "Only open broker orders can be cancelled")
        self._transition(order_id, OrderState.CANCEL_PENDING, "cancel_requested")
        try:
            self.broker.cancel_order(BrokerCancelRequest(order.broker_order_id))
        except BrokerTimeout as error:
            self._transition(order_id, OrderState.UNKNOWN, error.code, error.code)
            return self.get(order_id)
        self._transition(order_id, OrderState.CANCELLED, "broker_cancelled")
        return self.sync(order_id)

    def modify(
        self, order_id: UUID, *, quantity: int | None = None, limit_price: Money | None = None
    ) -> StoredOrder:
        order = self.sync(order_id)
        if order.broker_order_id is None or order.state not in {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
        }:
            raise OmsError("modify_not_allowed", "Only open broker orders can be modified")
        if order.modification_count >= 2:
            raise OmsError("modification_limit", "Order modification limit reached")
        if quantity is not None and (
            quantity < order.filled_quantity or quantity > order.approved_quantity
        ):
            raise OmsError(
                "modified_quantity_invalid",
                "Modified quantity must retain fills and cannot exceed risk approval",
            )
        if self.clock.now() - order.updated_at < self.minimum_modify_wait:
            raise OmsError("modify_too_soon", "Minimum modification wait has not elapsed")
        self.broker.modify_order(BrokerModifyRequest(order.broker_order_id, quantity, limit_price))
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """
                UPDATE oms_orders SET approved_quantity=COALESCE(?, approved_quantity),
                    limit_price_paise=COALESCE(?, limit_price_paise),
                    modification_count=modification_count+1, updated_at=? WHERE order_id=?
                """,
                (
                    quantity,
                    _paise(limit_price) if limit_price else None,
                    _aware_iso(self.clock.now()),
                    str(order_id),
                ),
            )
            self._event(
                connection,
                order_id,
                order.state,
                order.state,
                "order_modified",
                {
                    "quantity": quantity,
                    "limit_price_paise": _paise(limit_price) if limit_price else None,
                },
            )
        return self.get(order_id)

    def reconcile_unknown(self, order_id: UUID) -> StoredOrder:
        order = self.get(order_id)
        if order.state is not OrderState.RECONCILIATION_REQUIRED:
            raise OmsError("reconciliation_not_required", "Order is not awaiting reconciliation")
        found = next(
            (
                item
                for item in self.broker.get_orders()
                if item.client_order_id in {order.client_order_id, ClientOrderId(order.broker_tag)}
            ),
            None,
        )
        if found is None:
            self._transition(order_id, OrderState.RISK_APPROVED, "confirmed_absent")
            self._resolve_incidents(order_id)
            return self.get(order_id)
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE oms_orders SET broker_order_id=? WHERE order_id=?",
                (str(found.broker_order_id), str(order_id)),
            )
        self._transition(order_id, _state_from_broker(found.status), "reconciled_found")
        self._resolve_incidents(order_id)
        return self.sync(order_id)

    def open_orders(self) -> tuple[StoredOrder, ...]:
        connection = self.database.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT order_id FROM oms_orders
                WHERE state IN (
                    'OPEN', 'PARTIALLY_FILLED', 'CANCEL_PENDING',
                    'RECONCILIATION_REQUIRED'
                )
                ORDER BY created_at
                """
            ).fetchall()
            return tuple(
                self._get_with_connection(connection, UUID(row["order_id"])) for row in rows
            )
        finally:
            connection.close()

    def get(self, order_id: UUID) -> StoredOrder:
        connection = self.database.connect(read_only=True)
        try:
            return self._get_with_connection(connection, order_id)
        finally:
            connection.close()

    def _get_with_connection(self, connection: sqlite3.Connection, order_id: UUID) -> StoredOrder:
        row = connection.execute(
            "SELECT * FROM oms_orders WHERE order_id=?", (str(order_id),)
        ).fetchone()
        if row is None:
            raise OmsError("order_not_found", "OMS order was not found")
        return StoredOrder(
            UUID(row["order_id"]),
            row["idempotency_key"],
            UUID(row["risk_decision_id"]),
            InstrumentKey(row["instrument_key"]),
            OrderSide(row["side"]),
            int(row["requested_quantity"]),
            int(row["approved_quantity"]),
            _money_from_paise(row["limit_price_paise"]),
            OrderState(row["state"]),
            ClientOrderId(row["client_order_id"]),
            row["broker_tag"],
            BrokerOrderId(row["broker_order_id"]) if row["broker_order_id"] else None,
            int(row["filled_quantity"]),
            _money_from_paise(row["average_price_paise"]),
            int(row["modification_count"]),
            datetime.fromisoformat(row["updated_at"]),
        )

    def _transition(
        self, order_id: UUID, target: OrderState, reason: str, error_code: str | None = None
    ) -> None:
        with self.database.transaction(write=True) as connection:
            current = OrderState(
                connection.execute(
                    "SELECT state FROM oms_orders WHERE order_id=?", (str(order_id),)
                ).fetchone()[0]
            )
            if target not in TRANSITIONS[current]:
                raise OmsError(
                    "transition_invalid", f"Invalid order transition: {current} -> {target}"
                )
            now = _aware_iso(self.clock.now())
            connection.execute(
                "UPDATE oms_orders SET state=?, updated_at=?, last_error_code=? WHERE order_id=?",
                (target.value, now, error_code, str(order_id)),
            )
            self._event(connection, order_id, current, target, reason, {})

    def _event(
        self,
        connection: sqlite3.Connection,
        order_id: UUID,
        source: OrderState | None,
        target: OrderState,
        reason: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO oms_state_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(order_id),
                source.value if source else None,
                target.value,
                reason,
                json.dumps(payload, sort_keys=True),
                _aware_iso(self.clock.now()),
            ),
        )

    def _record_fill(self, order_id: UUID, trade: BrokerTrade) -> None:
        payload_hash = _trade_hash(trade)
        with self.database.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT payload_hash FROM oms_fills WHERE fill_id=?", (str(trade.fill_id),)
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise OmsError("fill_conflict", "Broker fill ID has different economics")
                return
            order = self._get_with_connection(connection, order_id)
            new_filled = order.filled_quantity + trade.quantity
            if new_filled > order.approved_quantity:
                raise OmsError("overfill", "Broker fills exceed approved quantity")
            total_value = (
                order.average_price.amount * order.filled_quantity
                + trade.price.amount * trade.quantity
            )
            average = Money(total_value / Decimal(new_filled))
            connection.execute(
                "INSERT INTO oms_fills VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(trade.fill_id),
                    str(order_id),
                    str(trade.broker_order_id),
                    trade.quantity,
                    _paise(trade.price),
                    _aware_iso(trade.filled_at),
                    payload_hash,
                ),
            )
            connection.execute(
                """
                UPDATE oms_orders
                SET filled_quantity=?, average_price_paise=?, updated_at=?
                WHERE order_id=?
                """,
                (new_filled, _paise(average), _aware_iso(self.clock.now()), str(order_id)),
            )
        if self.accounting is not None:
            accounting_fill = AccountingFill(
                FillId(str(trade.fill_id)),
                trade.broker_order_id,
                trade.instrument,
                trade.side,
                trade.quantity,
                trade.price,
                trade.filled_at,
            )
            costs = (
                self.fill_cost_estimator(accounting_fill, self.accounting)
                if self.fill_cost_estimator is not None
                else ()
            )
            applied = self.accounting.apply_fill(accounting_fill, costs)
            if applied and costs and self.cost_debiter is not None:
                total = sum((item.amount.amount for item in costs), Decimal(0))
                self.cost_debiter(Money(total))
        refreshed = self.get(order_id)
        target = (
            OrderState.FILLED
            if refreshed.filled_quantity == refreshed.approved_quantity
            else OrderState.PARTIALLY_FILLED
        )
        if target != refreshed.state and target in TRANSITIONS[refreshed.state]:
            self._transition(order_id, target, "fill_recorded")

    def _incident(self, order_id: UUID, code: str, detail: str) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO oms_incidents VALUES (?, ?, ?, ?, ?, NULL)",
                (str(uuid4()), str(order_id), code, detail, _aware_iso(self.clock.now())),
            )

    def _resolve_incidents(self, order_id: UUID) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE oms_incidents SET resolved_at=? WHERE order_id=? AND resolved_at IS NULL",
                (_aware_iso(self.clock.now()), str(order_id)),
            )


def _client_identity(intent: OmsIntent) -> tuple[ClientOrderId, str]:
    digest = hashlib.sha256(intent.idempotency_key.encode()).hexdigest()
    return ClientOrderId(f"PQ-{digest[:20]}"), f"PQ{digest[:18]}"


def _state_from_broker(status: BrokerOrderStatus) -> OrderState:
    return {
        BrokerOrderStatus.OPEN: OrderState.OPEN,
        BrokerOrderStatus.PARTIALLY_FILLED: OrderState.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED: OrderState.FILLED,
        BrokerOrderStatus.CANCELLED: OrderState.CANCELLED,
        BrokerOrderStatus.REJECTED: OrderState.REJECTED,
        BrokerOrderStatus.UNKNOWN: OrderState.RECONCILIATION_REQUIRED,
    }[status]


def _trade_hash(trade: BrokerTrade) -> str:
    raw = (
        str(trade.broker_order_id),
        str(trade.instrument),
        trade.side.value,
        trade.quantity,
        str(trade.price.amount),
        _aware_iso(trade.filled_at),
    )
    return hashlib.sha256(json.dumps(raw).encode()).hexdigest()


def _paise(value: Money) -> int:
    return int((value.amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _money_from_paise(value: int) -> Money:
    return Money(Decimal(value) / 100)


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OmsError("oms_time_naive", "OMS timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()
