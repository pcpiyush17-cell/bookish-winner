from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from personal_quant.broker.contracts import OrderSide
from personal_quant.config import OperatingMode
from personal_quant.domain.identifiers import InstrumentKey, StrategyId
from personal_quant.domain.money import Money
from personal_quant.risk import (
    CircuitBreaker,
    CircuitTrigger,
    KillSwitch,
    OrderIntent,
    RiskConfig,
    RiskContext,
    RiskDecisionType,
    RiskEngine,
    RiskError,
    RiskReason,
)
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

CONFIG = Path("config/risk/conservative_10k.yaml")
NOW = datetime(2026, 7, 29, 5, tzinfo=UTC)


def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "trading.sqlite")
    MigrationRunner(value).apply_all()
    return value


def intent(*, quantity: int = 10, key: str | None = None) -> OrderIntent:
    return OrderIntent(
        uuid4(),
        key or uuid4().hex,
        StrategyId("approved-strategy"),
        InstrumentKey("NSE:INFY"),
        OrderSide.BUY,
        quantity,
        Money.from_value("100"),
        Money.from_value("99"),
        Money.from_value("0.05"),
        Money.from_value("50"),
        Money.from_value("10"),
        NOW + timedelta(minutes=1),
    )


def context(**changes: object) -> RiskContext:
    values: dict[str, object] = {
        "now": NOW,
        "mode": OperatingMode.PAPER,
        "live_authorised": False,
        "authentication_valid": True,
        "account_matches": True,
        "public_ip_valid": False,
        "exchange": "NSE",
        "product": "CNC",
        "instrument_approved": True,
        "instrument_tradable": True,
        "quote_at": NOW - timedelta(seconds=1),
        "clock_skew_seconds": Decimal(0),
        "bid": Money.from_value("99.95"),
        "ask": Money.from_value("100.05"),
        "session_allowed": True,
        "strategy_approved": True,
        "reference_price": Money.from_value("100"),
        "max_price_deviation_pct": Decimal("2"),
        "open_positions": 0,
        "current_instrument_quantity": 0,
        "current_instrument_exposure": Money.from_value(0),
        "gross_exposure": Money.from_value(0),
        "available_cash": Money.from_value("10000"),
        "daily_realised_pnl": Money.from_value(0),
        "daily_total_pnl": Money.from_value(0),
        "monthly_drawdown_pct": Decimal(0),
        "orders_today": 0,
        "new_orders_last_minute": 0,
        "modification_count": 0,
        "reconciliation_healthy": True,
        "unresolved_critical_incident": False,
    }
    values.update(changes)
    return RiskContext(**values)  # type: ignore[arg-type]


def engine(tmp_path: Path) -> RiskEngine:
    return RiskEngine(database(tmp_path), RiskConfig.load(CONFIG))


def test_approved_resized_and_duplicate_decisions_are_persisted(tmp_path: Path) -> None:
    risk = engine(tmp_path)
    approved_intent = intent(quantity=10, key="same-economics")
    approved = risk.evaluate(approved_intent, context())
    assert approved.decision is RiskDecisionType.APPROVED
    assert approved.approved_quantity == 10
    resized = risk.evaluate(intent(quantity=100), context())
    assert resized.decision is RiskDecisionType.RESIZED
    assert resized.approved_quantity == 50
    assert set(resized.reasons) == {
        RiskReason.SINGLE_EXPOSURE,
        RiskReason.GROSS_EXPOSURE,
        RiskReason.ORDER_NOTIONAL,
    }
    duplicate = risk.evaluate(replace(approved_intent, intent_id=uuid4()), context())
    assert duplicate.decision is RiskDecisionType.REJECTED
    assert duplicate.reasons == (RiskReason.DUPLICATE_INTENT,)
    connection = risk.database.connect(read_only=True)
    try:
        count = connection.execute("SELECT count(*) FROM risk_decisions").fetchone()[0]
    finally:
        connection.close()
    assert count == 3


@pytest.mark.parametrize(
    ("change", "value", "reason"),
    [
        ("authentication_valid", False, RiskReason.AUTHENTICATION_INVALID),
        ("account_matches", False, RiskReason.ACCOUNT_MISMATCH),
        ("instrument_approved", False, RiskReason.INSTRUMENT_NOT_APPROVED),
        ("instrument_tradable", False, RiskReason.INSTRUMENT_NOT_TRADABLE),
        ("session_allowed", False, RiskReason.SESSION_CLOSED),
        ("strategy_approved", False, RiskReason.STRATEGY_NOT_APPROVED),
        ("reconciliation_healthy", False, RiskReason.RECONCILIATION_UNHEALTHY),
        ("unresolved_critical_incident", True, RiskReason.CRITICAL_INCIDENT),
        ("orders_today", 10, RiskReason.ORDER_FREQUENCY),
        ("monthly_drawdown_pct", Decimal("5"), RiskReason.MONTHLY_DRAWDOWN),
        ("daily_total_pnl", Money.from_value("-150"), RiskReason.DAILY_LOSS),
    ],
)
def test_each_failed_hard_gate_has_visible_reason(
    tmp_path: Path, change: str, value: object, reason: RiskReason
) -> None:
    decision = engine(tmp_path).evaluate(intent(), context(**{change: value}))
    assert decision.decision is RiskDecisionType.REJECTED
    assert reason in decision.reasons
    assert decision.approved_quantity == 0


def test_stale_crossed_and_live_safety_gates(tmp_path: Path) -> None:
    risk = engine(tmp_path)
    unsafe = context(
        mode=OperatingMode.LIVE,
        quote_at=NOW - timedelta(seconds=11),
        bid=Money.from_value("101"),
        ask=Money.from_value("100"),
    )
    decision = risk.evaluate(intent(), unsafe)
    assert set(decision.reasons) >= {
        RiskReason.LIVE_NOT_AUTHORISED,
        RiskReason.PUBLIC_IP_INVALID,
        RiskReason.DATA_STALE,
        RiskReason.MARKET_INCONSISTENT,
    }


def test_kill_switch_survives_restart_and_requires_safe_human_reset(tmp_path: Path) -> None:
    db = database(tmp_path)
    switch = KillSwitch(db)
    assert switch.activate("broker mismatch", NOW, trigger_code="broker_local_mismatch")
    assert not switch.activate("still unsafe", NOW)
    assert KillSwitch(Database(db.path)).active()
    decision = RiskEngine(Database(db.path), RiskConfig.load(CONFIG)).evaluate(intent(), context())
    assert RiskReason.KILL_SWITCH_ACTIVE in decision.reasons
    with pytest.raises(RiskError, match="requires"):
        switch.reset(NOW, reconciliation_healthy=False, human_authorised=True)
    switch.reset(NOW, reconciliation_healthy=True, human_authorised=True)
    assert not KillSwitch(Database(db.path)).active()


def test_circuit_breaker_trips_persistent_kill_switch(tmp_path: Path) -> None:
    switch = KillSwitch(database(tmp_path))
    assert CircuitBreaker(switch).trip(CircuitTrigger.DUPLICATE_FILL, "duplicate broker trade", NOW)
    assert switch.active()


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    requested=st.integers(min_value=1, max_value=500),
    cash=st.integers(min_value=0, max_value=20_000),
)
def test_approved_quantity_never_exceeds_any_limit(
    tmp_path: Path, requested: int, cash: int
) -> None:
    risk = engine(tmp_path)
    decision = risk.evaluate(
        intent(quantity=requested), context(available_cash=Money.from_value(cash))
    )
    assert 0 <= decision.approved_quantity <= requested
    if decision.approved_quantity:
        assert decision.approved_quantity * 100 <= 5000
        assert decision.approved_quantity * 100 + 10 <= max(cash - 1000, 0)


def test_invalid_config_and_naive_time_fail_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "risk.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(RiskError, match="cannot be loaded"):
        RiskConfig.load(invalid)
    with pytest.raises(RiskError, match="timezone-aware"):
        engine(tmp_path).evaluate(intent(), context(now=datetime.now()))
