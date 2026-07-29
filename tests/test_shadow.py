import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from personal_quant.broker.contracts import (
    BrokerHolding,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    FundsSnapshot,
    OrderSide,
)
from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import (
    BrokerOrderId,
    ClientOrderId,
    FillId,
    InstrumentKey,
)
from personal_quant.domain.money import Money
from personal_quant.shadow import (
    DifferenceKind,
    IntendedOrder,
    ShadowBrokerAdapter,
    ShadowError,
    ShadowService,
    compare_intended_orders,
)
from personal_quant.storage.database import Database
from personal_quant.storage.migrations import MigrationRunner

NOW = datetime(2026, 7, 29, 10, tzinfo=UTC)


def request(client_id: str, *, quantity: int = 2) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        ClientOrderId(client_id),
        InstrumentKey("NSE:INFY"),
        OrderSide.BUY,
        quantity,
        Money.from_value("1500.25"),
        "pqshadow01",
    )


def observed(client_id: str, *, quantity: int = 2) -> BrokerOrder:
    return BrokerOrder(
        BrokerOrderId(f"broker-{client_id}"),
        ClientOrderId(client_id),
        InstrumentKey("NSE:INFY"),
        OrderSide.BUY,
        quantity,
        0,
        quantity,
        Money.from_value("1500.25"),
        Money.from_value("0"),
        BrokerOrderStatus.OPEN,
        NOW,
    )


class ReadSource:
    def __init__(self, orders: tuple[BrokerOrder, ...] = ()) -> None:
        self.orders = orders
        self.calls: list[str] = []

    def get_profile(self) -> BrokerProfile:
        self.calls.append("profile")
        return BrokerProfile("SHADOWUSER", "Shadow User", "ZERODHA", ("NSE",), ("CNC",))

    def get_funds(self) -> FundsSnapshot:
        self.calls.append("funds")
        return FundsSnapshot(Money.from_value("10000"))

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        self.calls.append("positions")
        return (BrokerPosition(InstrumentKey("NSE:INFY"), 1, Money.from_value("1400")),)

    def get_holdings(self) -> tuple[BrokerHolding, ...]:
        self.calls.append("holdings")
        return (BrokerHolding(InstrumentKey("NSE:INFY"), 1, Money.from_value("1400")),)

    def get_orders(self) -> tuple[BrokerOrder, ...]:
        self.calls.append("orders")
        return self.orders

    def get_trades(self) -> tuple[BrokerTrade, ...]:
        self.calls.append("trades")
        return (
            BrokerTrade(
                FillId("fill-1"),
                BrokerOrderId("old-order"),
                InstrumentKey("NSE:INFY"),
                OrderSide.BUY,
                1,
                Money.from_value("1400"),
                NOW,
            ),
        )


def test_shadow_adapter_exposes_reads_but_no_transmission_path() -> None:
    source = ReadSource()
    adapter = ShadowBrokerAdapter(source, SimulatedClock(NOW))

    snapshot = adapter.capture()

    assert snapshot.profile.user_id == "SHADOWUSER"
    assert source.calls == ["profile", "funds", "positions", "holdings", "orders", "trades"]
    assert not hasattr(adapter, "place_order")
    assert not hasattr(adapter, "modify_order")
    assert not hasattr(adapter, "cancel_order")


def test_intended_comparison_reports_matches_mismatches_missing_and_unexpected() -> None:
    intents = (
        IntendedOrder(request("match"), NOW, "baseline"),
        IntendedOrder(request("changed"), NOW, "baseline"),
        IntendedOrder(request("missing"), NOW, "baseline"),
    )
    differences = compare_intended_orders(
        intents,
        (observed("match"), observed("changed", quantity=3), observed("unexpected")),
    )

    assert [item.kind for item in differences] == [
        DifferenceKind.MATCHED,
        DifferenceKind.ECONOMICS_MISMATCH,
        DifferenceKind.MISSING_AT_BROKER,
        DifferenceKind.UNEXPECTED_AT_BROKER,
    ]
    assert differences[1].fields == ("quantity",)


def test_comparison_rejects_duplicate_intents_and_blank_strategy() -> None:
    with pytest.raises(ValueError, match="strategy_id"):
        IntendedOrder(request("one"), NOW, " ")
    duplicate = IntendedOrder(request("one"), NOW, "baseline")
    with pytest.raises(ValueError, match="duplicate"):
        compare_intended_orders((duplicate, duplicate), ())


def test_shadow_service_persists_immutable_snapshot_and_difference_report(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    adapter = ShadowBrokerAdapter(ReadSource((observed("match"),)), SimulatedClock(NOW))
    service = ShadowService(
        database,
        adapter,
        SimulatedClock(NOW),
        tmp_path / "reports",
        require_paper_acceptance=False,
    )

    report, path = service.run((IntendedOrder(request("match"), NOW, "baseline"),))

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["mode"] == "shadow"
    assert document["transmission_available"] is False
    assert document["snapshot"]["funds"]["available_cash"]["amount"] == "10000.00"
    assert report.has_differences is False
    connection = database.connect(read_only=True)
    try:
        row = connection.execute(
            "SELECT difference_count, report_sha256 FROM shadow_reports WHERE report_id=?",
            (str(report.report_id),),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == (0, hashlib.sha256(path.read_bytes()).hexdigest())
    assert path.name == f"shadow-{report.report_id}.json"


def test_missing_intent_marks_report_as_different(tmp_path: Path) -> None:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    service = ShadowService(
        database,
        ShadowBrokerAdapter(ReadSource(), SimulatedClock(NOW)),
        SimulatedClock(NOW),
        tmp_path / str(uuid4()),
        require_paper_acceptance=False,
    )
    report, _ = service.run((IntendedOrder(request("missing"), NOW, "baseline"),))
    assert report.has_differences is True


def test_shadow_service_fails_closed_while_wp14_acceptance_is_pending(tmp_path: Path) -> None:
    database = Database(tmp_path / "trading.sqlite")
    MigrationRunner(database).apply_all()
    source = ReadSource()
    service = ShadowService(
        database,
        ShadowBrokerAdapter(source, SimulatedClock(NOW)),
        SimulatedClock(NOW),
        tmp_path / "reports",
    )

    with pytest.raises(ShadowError) as captured:
        service.run(())

    assert captured.value.code == "paper_acceptance_pending"
    assert source.calls == []
