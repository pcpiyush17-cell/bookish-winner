"""Read-only broker shadow mode and intended-order difference reporting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from personal_quant.broker.contracts import (
    BrokerHolding,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerPosition,
    BrokerProfile,
    BrokerTrade,
    FundsSnapshot,
)
from personal_quant.clocks import Clock
from personal_quant.paper_runtime import runtime_progress
from personal_quant.storage.database import Database


class ShadowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ReadOnlyBroker(Protocol):
    """The complete and intentionally non-trading broker surface for shadow mode."""

    def get_profile(self) -> BrokerProfile: ...

    def get_funds(self) -> FundsSnapshot: ...

    def get_positions(self) -> Sequence[BrokerPosition]: ...

    def get_holdings(self) -> Sequence[BrokerHolding]: ...

    def get_orders(self) -> Sequence[BrokerOrder]: ...

    def get_trades(self) -> Sequence[BrokerTrade]: ...


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    captured_at: datetime
    profile: BrokerProfile
    funds: FundsSnapshot
    positions: tuple[BrokerPosition, ...]
    holdings: tuple[BrokerHolding, ...]
    orders: tuple[BrokerOrder, ...]
    trades: tuple[BrokerTrade, ...]


class ShadowBrokerAdapter:
    """Expose broker reads without any order-transmission method."""

    def __init__(self, source: ReadOnlyBroker, clock: Clock) -> None:
        self._source = source
        self._clock = clock

    def capture(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            captured_at=self._clock.now(),
            profile=self._source.get_profile(),
            funds=self._source.get_funds(),
            positions=tuple(self._source.get_positions()),
            holdings=tuple(self._source.get_holdings()),
            orders=tuple(self._source.get_orders()),
            trades=tuple(self._source.get_trades()),
        )


@dataclass(frozen=True, slots=True)
class IntendedOrder:
    request: BrokerOrderRequest
    created_at: datetime
    strategy_id: str

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")


class DifferenceKind(StrEnum):
    MATCHED = "matched"
    MISSING_AT_BROKER = "missing_at_broker"
    ECONOMICS_MISMATCH = "economics_mismatch"
    UNEXPECTED_AT_BROKER = "unexpected_at_broker"


@dataclass(frozen=True, slots=True)
class OrderDifference:
    kind: DifferenceKind
    client_order_id: str | None
    broker_order_id: str | None
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShadowReport:
    report_id: UUID
    created_at: datetime
    snapshot: BrokerSnapshot
    intended_count: int
    differences: tuple[OrderDifference, ...]

    @property
    def has_differences(self) -> bool:
        return any(item.kind is not DifferenceKind.MATCHED for item in self.differences)


def compare_intended_orders(
    intended: Sequence[IntendedOrder],
    broker_orders: Sequence[BrokerOrder],
) -> tuple[OrderDifference, ...]:
    """Compare intent economics with observed broker orders without transmitting anything."""
    observed = {
        str(order.client_order_id): order
        for order in broker_orders
        if order.client_order_id is not None
    }
    intended_ids: set[str] = set()
    differences: list[OrderDifference] = []
    for item in intended:
        request = item.request
        client_id = str(request.client_order_id)
        if client_id in intended_ids:
            raise ValueError("duplicate intended client_order_id")
        intended_ids.add(client_id)
        broker_order = observed.get(client_id)
        if broker_order is None:
            differences.append(OrderDifference(DifferenceKind.MISSING_AT_BROKER, client_id, None))
            continue
        fields = tuple(
            name
            for name, matches in (
                ("instrument", broker_order.instrument == request.instrument),
                ("side", broker_order.side is request.side),
                ("quantity", broker_order.quantity == request.quantity),
                ("limit_price", broker_order.limit_price == request.limit_price),
            )
            if not matches
        )
        differences.append(
            OrderDifference(
                DifferenceKind.ECONOMICS_MISMATCH if fields else DifferenceKind.MATCHED,
                client_id,
                str(broker_order.broker_order_id),
                fields,
            )
        )
    for client_id, broker_order in observed.items():
        if client_id not in intended_ids:
            differences.append(
                OrderDifference(
                    DifferenceKind.UNEXPECTED_AT_BROKER,
                    client_id,
                    str(broker_order.broker_order_id),
                )
            )
    for broker_order in broker_orders:
        if broker_order.client_order_id is None:
            differences.append(
                OrderDifference(
                    DifferenceKind.UNEXPECTED_AT_BROKER,
                    None,
                    str(broker_order.broker_order_id),
                )
            )
    return tuple(differences)


class ShadowService:
    """Capture one read-only broker snapshot and persist its difference report."""

    def __init__(
        self,
        database: Database,
        broker: ShadowBrokerAdapter,
        clock: Clock,
        report_root: Path,
        *,
        require_paper_acceptance: bool = True,
    ) -> None:
        self._database = database
        self._broker = broker
        self._clock = clock
        self._report_root = report_root
        self._require_paper_acceptance = require_paper_acceptance

    def run(self, intended: Sequence[IntendedOrder]) -> tuple[ShadowReport, Path]:
        if self._require_paper_acceptance:
            progress = runtime_progress(self._database)
            if not progress.dry_requirement_met or not progress.formal_requirement_met:
                raise ShadowError(
                    "paper_acceptance_pending",
                    "Shadow mode requires 10 clean dry and 30 clean formal paper sessions",
                )
        snapshot = self._broker.capture()
        report = ShadowReport(
            report_id=uuid4(),
            created_at=self._clock.now(),
            snapshot=snapshot,
            intended_count=len(intended),
            differences=compare_intended_orders(intended, snapshot.orders),
        )
        payload = _report_document(report)
        encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        checksum = hashlib.sha256(encoded).hexdigest()
        path = self._write(report, encoded)
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO shadow_reports (
                    report_id, captured_at, broker_user_id, intended_count,
                    difference_count, snapshot_json, difference_json, report_path,
                    report_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(report.report_id),
                    report.created_at.isoformat(),
                    report.snapshot.profile.user_id,
                    report.intended_count,
                    sum(item.kind is not DifferenceKind.MATCHED for item in report.differences),
                    json.dumps(payload["snapshot"], sort_keys=True),
                    json.dumps(payload["differences"], sort_keys=True),
                    str(path),
                    checksum,
                ),
            )
        return report, path

    def _write(self, report: ShadowReport, encoded: bytes) -> Path:
        directory = self._report_root / report.created_at.date().isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"shadow-{report.report_id}.json"
        with path.open("xb") as stream:
            stream.write(encoded)
        return path


def _report_document(report: ShadowReport) -> dict[str, object]:
    return {
        "schema_version": 1,
        "report_id": str(report.report_id),
        "created_at": report.created_at.isoformat(),
        "mode": "shadow",
        "transmission_available": False,
        "intended_count": report.intended_count,
        "snapshot": _jsonable(asdict(report.snapshot)),
        "differences": [_jsonable(asdict(item)) for item in report.differences],
    }


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, UUID, StrEnum)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "amount") and hasattr(value, "currency"):
        return {"amount": str(value.amount), "currency": str(value.currency)}
    return value
