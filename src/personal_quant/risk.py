"""Fail-closed pre-trade risk decisions, persistent kill switch, and circuits."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_quant.broker.contracts import OrderSide
from personal_quant.config import OperatingMode
from personal_quant.domain.identifiers import InstrumentKey, StrategyId
from personal_quant.domain.money import Money
from personal_quant.storage.database import Database


class RiskError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class StrictRiskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExposureConfig(StrictRiskModel):
    max_gross_exposure_inr: Decimal = Field(gt=0)
    max_single_position_inr: Decimal = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    cash_reserve_inr: Decimal = Field(ge=0)
    allow_leverage: bool
    allow_short_positions: bool
    allowed_products: tuple[str, ...]
    allowed_exchanges: tuple[str, ...]

    @field_validator(
        "max_gross_exposure_inr", "max_single_position_inr", "cash_reserve_inr", mode="before"
    )
    @classmethod
    def decimals(cls, value: object) -> object:
        return _decimal_string(value)

    @field_validator("allowed_products", "allowed_exchanges", mode="before")
    @classmethod
    def tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class LossLimitConfig(StrictRiskModel):
    max_loss_per_trade_inr: Decimal = Field(gt=0)
    max_daily_realised_loss_inr: Decimal = Field(gt=0)
    max_daily_total_loss_inr: Decimal = Field(gt=0)
    max_monthly_drawdown_pct: Decimal = Field(gt=0)

    @field_validator("*", mode="before")
    @classmethod
    def decimals(cls, value: object) -> object:
        return _decimal_string(value)


class OrderLimitConfig(StrictRiskModel):
    max_orders_per_day: int = Field(gt=0)
    max_new_orders_per_minute: int = Field(gt=0)
    max_modifications_per_order: int = Field(ge=0)
    max_notional_per_order_inr: Decimal = Field(gt=0)
    duplicate_intent_window_seconds: int = Field(gt=0)
    limit_order_only: bool

    @field_validator("max_notional_per_order_inr", mode="before")
    @classmethod
    def decimals(cls, value: object) -> object:
        return _decimal_string(value)


class DataSafetyConfig(StrictRiskModel):
    max_quote_age_seconds: int = Field(gt=0)
    max_clock_skew_seconds: int = Field(ge=0)
    require_bid_ask: bool
    reject_crossed_market: bool


class EdgeConfig(StrictRiskModel):
    min_edge_to_cost_ratio: Decimal = Field(gt=0)
    min_net_edge_inr: Decimal = Field(ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def decimals(cls, value: object) -> object:
        return _decimal_string(value)


class RiskConfig(StrictRiskModel):
    schema_version: int = Field(ge=1)
    name: str = Field(min_length=1)
    capital_reference_inr: Decimal = Field(gt=0)
    exposure: ExposureConfig
    loss_limits: LossLimitConfig
    order_limits: OrderLimitConfig
    data_safety: DataSafetyConfig
    edge: EdgeConfig

    @field_validator("capital_reference_inr", mode="before")
    @classmethod
    def capital(cls, value: object) -> object:
        return _decimal_string(value)

    @classmethod
    def load(cls, path: Path) -> RiskConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise RiskError("risk_config_invalid", "Risk configuration cannot be loaded") from error

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class RiskDecisionType(StrEnum):
    APPROVED = "approved"
    RESIZED = "resized"
    REJECTED = "rejected"


class RiskReason(StrEnum):
    MODE_INVALID = "mode_invalid"
    LIVE_NOT_AUTHORISED = "live_not_authorised"
    AUTHENTICATION_INVALID = "authentication_invalid"
    ACCOUNT_MISMATCH = "account_mismatch"
    PUBLIC_IP_INVALID = "public_ip_invalid"
    EXCHANGE_NOT_ALLOWED = "exchange_not_allowed"
    PRODUCT_NOT_ALLOWED = "product_not_allowed"
    INSTRUMENT_NOT_APPROVED = "instrument_not_approved"
    INSTRUMENT_NOT_TRADABLE = "instrument_not_tradable"
    DATA_STALE = "data_stale"
    MARKET_INCONSISTENT = "market_inconsistent"
    SESSION_CLOSED = "session_closed"
    STRATEGY_NOT_APPROVED = "strategy_not_approved"
    SIGNAL_EXPIRED = "signal_expired"
    DUPLICATE_INTENT = "duplicate_intent"
    QUANTITY_INVALID = "quantity_invalid"
    TICK_INVALID = "tick_invalid"
    PRICE_DEVIATION = "price_deviation"
    POSITION_COUNT = "position_count"
    SINGLE_EXPOSURE = "single_exposure"
    GROSS_EXPOSURE = "gross_exposure"
    CASH_RESERVE = "cash_reserve"
    PER_TRADE_RISK = "per_trade_risk"
    DAILY_LOSS = "daily_loss"
    MONTHLY_DRAWDOWN = "monthly_drawdown"
    ORDER_FREQUENCY = "order_frequency"
    ORDER_NOTIONAL = "order_notional"
    MODIFICATION_LIMIT = "modification_limit"
    EXPECTED_EDGE = "expected_edge"
    RECONCILIATION_UNHEALTHY = "reconciliation_unhealthy"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    CRITICAL_INCIDENT = "critical_incident"


class CircuitTrigger(StrEnum):
    STALE_MARKET_DATA = "stale_market_data"
    BROKER_LOCAL_MISMATCH = "broker_local_mismatch"
    WRONG_ACCOUNT = "wrong_account"
    DAILY_LOSS_BREACH = "daily_loss_breach"
    DUPLICATE_FILL = "duplicate_fill"
    DATABASE_FAILURE = "database_failure"
    UNHANDLED_EXCEPTION = "unhandled_exception"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: UUID
    idempotency_key: str
    strategy_id: StrategyId
    instrument_key: InstrumentKey
    side: OrderSide
    quantity: int
    limit_price: Money
    stop_price: Money
    tick_size: Money
    expected_gross_edge: Money
    expected_costs: Money
    signal_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    mode: OperatingMode
    live_authorised: bool
    authentication_valid: bool
    account_matches: bool
    public_ip_valid: bool
    exchange: str
    product: str
    instrument_approved: bool
    instrument_tradable: bool
    quote_at: datetime
    clock_skew_seconds: Decimal
    bid: Money | None
    ask: Money | None
    session_allowed: bool
    strategy_approved: bool
    reference_price: Money
    max_price_deviation_pct: Decimal
    open_positions: int
    current_instrument_quantity: int
    current_instrument_exposure: Money
    gross_exposure: Money
    available_cash: Money
    daily_realised_pnl: Money
    daily_total_pnl: Money
    monthly_drawdown_pct: Decimal
    orders_today: int
    new_orders_last_minute: int
    modification_count: int
    reconciliation_healthy: bool
    unresolved_critical_incident: bool


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: UUID
    intent_id: UUID
    decision: RiskDecisionType
    requested_quantity: int
    approved_quantity: int
    reasons: tuple[RiskReason, ...]
    evaluated_at: datetime
    config_hash: str


@dataclass(frozen=True, slots=True)
class KillSwitch:
    database: Database

    def active(self) -> bool:
        connection = self.database.connect(read_only=True)
        try:
            return bool(
                connection.execute(
                    "SELECT active FROM kill_switch_state WHERE singleton=1"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def activate(self, reason: str, occurred_at: datetime, *, trigger_code: str = "manual") -> bool:
        if not reason.strip():
            raise RiskError("kill_switch_reason_missing", "Kill-switch reason is required")
        timestamp = _aware_iso(occurred_at)
        with self.database.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT active FROM kill_switch_state WHERE singleton=1"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO circuit_breaker_events VALUES (?, ?, ?, ?)",
                (str(uuid4()), trigger_code, reason, timestamp),
            )
            if current:
                return False
            connection.execute(
                """
                UPDATE kill_switch_state
                SET active=1, reason=?, activated_at=?, reset_at=NULL
                WHERE singleton=1
                """,
                (reason, timestamp),
            )
            return True

    def reset(
        self, occurred_at: datetime, *, reconciliation_healthy: bool, human_authorised: bool
    ) -> None:
        if not reconciliation_healthy or not human_authorised:
            raise RiskError(
                "kill_switch_reset_denied", "Reset requires reconciliation and human authorisation"
            )
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE kill_switch_state SET active=0, reason='', reset_at=? WHERE singleton=1",
                (_aware_iso(occurred_at),),
            )


@dataclass(frozen=True, slots=True)
class CircuitBreaker:
    kill_switch: KillSwitch

    def trip(self, trigger: CircuitTrigger, detail: str, occurred_at: datetime) -> bool:
        return self.kill_switch.activate(detail, occurred_at, trigger_code=trigger.value)


@dataclass(frozen=True, slots=True)
class RiskEngine:
    database: Database
    config: RiskConfig

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskDecision:
        now = _aware(context.now)
        _aware(intent.signal_expires_at)
        _aware(context.quote_at)
        reasons = self._hard_gate_reasons(intent, context)
        approved = 0
        resize_reasons: tuple[RiskReason, ...] = ()
        if not reasons:
            approved, sizing_reason, resize_reasons = self._size(intent, context)
            if sizing_reason is not None:
                reasons.append(sizing_reason)
        decision_type = (
            RiskDecisionType.REJECTED
            if reasons
            else RiskDecisionType.RESIZED
            if approved < intent.quantity
            else RiskDecisionType.APPROVED
        )
        decision = RiskDecision(
            uuid4(),
            intent.intent_id,
            decision_type,
            intent.quantity,
            0 if reasons else approved,
            tuple(reasons) if reasons else resize_reasons,
            now,
            self.config.fingerprint(),
        )
        self._persist(decision, intent, context)
        return decision

    def _hard_gate_reasons(self, intent: OrderIntent, context: RiskContext) -> list[RiskReason]:
        cfg = self.config
        checks: list[tuple[bool, RiskReason]] = [
            (
                context.mode not in {OperatingMode.PAPER, OperatingMode.SHADOW, OperatingMode.LIVE},
                RiskReason.MODE_INVALID,
            ),
            (
                context.mode is OperatingMode.LIVE and not context.live_authorised,
                RiskReason.LIVE_NOT_AUTHORISED,
            ),
            (not context.authentication_valid, RiskReason.AUTHENTICATION_INVALID),
            (not context.account_matches, RiskReason.ACCOUNT_MISMATCH),
            (
                context.mode is OperatingMode.LIVE and not context.public_ip_valid,
                RiskReason.PUBLIC_IP_INVALID,
            ),
            (
                context.exchange not in cfg.exposure.allowed_exchanges,
                RiskReason.EXCHANGE_NOT_ALLOWED,
            ),
            (context.product not in cfg.exposure.allowed_products, RiskReason.PRODUCT_NOT_ALLOWED),
            (not context.instrument_approved, RiskReason.INSTRUMENT_NOT_APPROVED),
            (not context.instrument_tradable, RiskReason.INSTRUMENT_NOT_TRADABLE),
            (
                (context.now - context.quote_at).total_seconds()
                > cfg.data_safety.max_quote_age_seconds
                or abs(context.clock_skew_seconds) > cfg.data_safety.max_clock_skew_seconds,
                RiskReason.DATA_STALE,
            ),
            (
                cfg.data_safety.require_bid_ask and (context.bid is None or context.ask is None),
                RiskReason.MARKET_INCONSISTENT,
            ),
            (
                cfg.data_safety.reject_crossed_market
                and context.bid is not None
                and context.ask is not None
                and context.bid.amount > context.ask.amount,
                RiskReason.MARKET_INCONSISTENT,
            ),
            (not context.session_allowed, RiskReason.SESSION_CLOSED),
            (not context.strategy_approved, RiskReason.STRATEGY_NOT_APPROVED),
            (context.now > intent.signal_expires_at, RiskReason.SIGNAL_EXPIRED),
            (self._is_duplicate(intent.idempotency_key, context.now), RiskReason.DUPLICATE_INTENT),
            (intent.quantity <= 0, RiskReason.QUANTITY_INVALID),
            (
                intent.limit_price.amount <= 0
                or intent.tick_size.amount <= 0
                or intent.limit_price.amount % intent.tick_size.amount != 0,
                RiskReason.TICK_INVALID,
            ),
            (
                _price_deviation(intent.limit_price, context.reference_price)
                > context.max_price_deviation_pct,
                RiskReason.PRICE_DEVIATION,
            ),
            (
                context.open_positions >= cfg.exposure.max_open_positions
                and context.current_instrument_quantity == 0
                and intent.side is OrderSide.BUY,
                RiskReason.POSITION_COUNT,
            ),
            (
                context.daily_realised_pnl.amount <= -cfg.loss_limits.max_daily_realised_loss_inr
                or context.daily_total_pnl.amount <= -cfg.loss_limits.max_daily_total_loss_inr,
                RiskReason.DAILY_LOSS,
            ),
            (
                context.monthly_drawdown_pct >= cfg.loss_limits.max_monthly_drawdown_pct,
                RiskReason.MONTHLY_DRAWDOWN,
            ),
            (
                context.orders_today >= cfg.order_limits.max_orders_per_day
                or context.new_orders_last_minute >= cfg.order_limits.max_new_orders_per_minute,
                RiskReason.ORDER_FREQUENCY,
            ),
            (
                context.modification_count > cfg.order_limits.max_modifications_per_order,
                RiskReason.MODIFICATION_LIMIT,
            ),
            (not context.reconciliation_healthy, RiskReason.RECONCILIATION_UNHEALTHY),
            (KillSwitch(self.database).active(), RiskReason.KILL_SWITCH_ACTIVE),
            (context.unresolved_critical_incident, RiskReason.CRITICAL_INCIDENT),
        ]
        return list(dict.fromkeys(reason for failed, reason in checks if failed))

    def _size(
        self, intent: OrderIntent, context: RiskContext
    ) -> tuple[int, RiskReason | None, tuple[RiskReason, ...]]:
        cfg = self.config
        if intent.side is OrderSide.SELL:
            if (
                not cfg.exposure.allow_short_positions
                and intent.quantity > context.current_instrument_quantity
            ):
                return 0, RiskReason.QUANTITY_INVALID, ()
            return intent.quantity, None, ()
        price = intent.limit_price.amount
        risk_per_share = abs(price - intent.stop_price.amount)
        if risk_per_share <= 0:
            return 0, RiskReason.PER_TRADE_RISK, ()
        risk_quantity = int(
            (cfg.loss_limits.max_loss_per_trade_inr / risk_per_share).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        cash_available = (
            context.available_cash.amount
            - cfg.exposure.cash_reserve_inr
            - intent.expected_costs.amount
        )
        cash_quantity = int(
            (max(cash_available, Decimal(0)) / price).to_integral_value(rounding=ROUND_FLOOR)
        )
        single_remaining = (
            cfg.exposure.max_single_position_inr - context.current_instrument_exposure.amount
        )
        single_quantity = int(
            (max(single_remaining, Decimal(0)) / price).to_integral_value(rounding=ROUND_FLOOR)
        )
        gross_remaining = cfg.exposure.max_gross_exposure_inr - context.gross_exposure.amount
        gross_quantity = int(
            (max(gross_remaining, Decimal(0)) / price).to_integral_value(rounding=ROUND_FLOOR)
        )
        notional_quantity = int(
            (cfg.order_limits.max_notional_per_order_inr / price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        approved = min(
            intent.quantity,
            risk_quantity,
            cash_quantity,
            single_quantity,
            gross_quantity,
            notional_quantity,
        )
        if approved <= 0:
            if risk_quantity <= 0:
                return 0, RiskReason.PER_TRADE_RISK, ()
            if cash_quantity <= 0:
                return 0, RiskReason.CASH_RESERVE, ()
            if single_quantity <= 0:
                return 0, RiskReason.SINGLE_EXPOSURE, ()
            return 0, RiskReason.GROSS_EXPOSURE, ()
        net_edge = intent.expected_gross_edge.amount - intent.expected_costs.amount
        ratio = (
            intent.expected_gross_edge.amount / intent.expected_costs.amount
            if intent.expected_costs.amount > 0
            else Decimal("Infinity")
        )
        if net_edge < cfg.edge.min_net_edge_inr or ratio < cfg.edge.min_edge_to_cost_ratio:
            return 0, RiskReason.EXPECTED_EDGE, ()
        constraints = (
            (risk_quantity, RiskReason.PER_TRADE_RISK),
            (cash_quantity, RiskReason.CASH_RESERVE),
            (single_quantity, RiskReason.SINGLE_EXPOSURE),
            (gross_quantity, RiskReason.GROSS_EXPOSURE),
            (notional_quantity, RiskReason.ORDER_NOTIONAL),
        )
        resize_reasons = tuple(
            reason
            for quantity, reason in constraints
            if approved < intent.quantity and quantity == approved
        )
        return approved, None, resize_reasons

    def _is_duplicate(self, key: str, now: datetime) -> bool:
        cutoff = _aware(now) - timedelta(
            seconds=self.config.order_limits.duplicate_intent_window_seconds
        )
        connection = self.database.connect(read_only=True)
        try:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM risk_decisions
                    WHERE idempotency_key=? AND evaluated_at>=? LIMIT 1
                    """,
                    (key, cutoff.isoformat()),
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def _persist(self, decision: RiskDecision, intent: OrderIntent, context: RiskContext) -> None:
        snapshot = _snapshot(intent, context)
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO risk_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(decision.decision_id),
                    str(decision.intent_id),
                    intent.idempotency_key,
                    decision.decision.value,
                    decision.requested_quantity,
                    decision.approved_quantity,
                    json.dumps([item.value for item in decision.reasons]),
                    decision.config_hash,
                    json.dumps(snapshot, sort_keys=True),
                    decision.evaluated_at.isoformat(),
                ),
            )


def _snapshot(intent: OrderIntent, context: RiskContext) -> dict[str, Any]:
    return {
        "intent": _json_safe(asdict(intent)),
        "context": _json_safe(asdict(context)),
    }


def _json_safe(value: object) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency.value}
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, (StrEnum, Decimal)):
        return str(value)
    return value


def _decimal_string(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("money and ratios must be quoted decimal strings")
    try:
        return Decimal(value)
    except InvalidOperation:
        return value


def _price_deviation(price: Money, reference: Money) -> Decimal:
    if reference.amount <= 0:
        return Decimal("Infinity")
    return abs(price.amount - reference.amount) / reference.amount * 100


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RiskError("risk_time_naive", "Risk timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _aware_iso(value: datetime) -> str:
    return _aware(value).isoformat()
