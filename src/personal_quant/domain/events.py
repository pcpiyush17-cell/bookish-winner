"""Metadata shared by immutable domain events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from personal_quant.domain.identifiers import RunId


class CoreEventType(StrEnum):
    """Stable names for the core event contracts in the blueprint."""

    MARKET_QUOTE_RECEIVED = "MarketQuoteReceived"
    MARKET_BAR_CLOSED = "MarketBarClosed"
    DATA_QUALITY_VIOLATION = "DataQualityViolation"
    FEATURE_VECTOR_CREATED = "FeatureVectorCreated"
    SIGNAL_CREATED = "SignalCreated"
    ORDER_INTENT_CREATED = "OrderIntentCreated"
    COST_ESTIMATE_CREATED = "CostEstimateCreated"
    RISK_DECISION_CREATED = "RiskDecisionCreated"
    ORDER_SUBMITTED = "OrderSubmitted"
    ORDER_ACKNOWLEDGED = "OrderAcknowledged"
    ORDER_REJECTED = "OrderRejected"
    ORDER_MODIFIED = "OrderModified"
    ORDER_CANCELLED = "OrderCancelled"
    ORDER_PARTIALLY_FILLED = "OrderPartiallyFilled"
    ORDER_FILLED = "OrderFilled"
    POSITION_CHANGED = "PositionChanged"
    CASH_CHANGED = "CashChanged"
    PNL_MARKED = "PnLMarked"
    RECONCILIATION_STARTED = "ReconciliationStarted"
    RECONCILIATION_DIFFERENCE_FOUND = "ReconciliationDifferenceFound"
    RECONCILIATION_COMPLETED = "ReconciliationCompleted"
    KILL_SWITCH_ACTIVATED = "KillSwitchActivated"
    RUNTIME_STARTED = "RuntimeStarted"
    RUNTIME_STOPPED = "RuntimeStopped"
    INCIDENT_RAISED = "IncidentRaised"


@dataclass(frozen=True, slots=True)
class EventMeta:
    """Trace and version metadata attached to every domain event."""

    event_id: UUID
    event_type: CoreEventType
    schema_version: int
    occurred_at: datetime
    received_at: datetime
    source: str
    correlation_id: UUID | None
    causation_id: UUID | None
    run_id: RunId

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("schema_version must be at least 1")
        if not self.source.strip():
            raise ValueError("source must not be blank")
        _require_aware(self.occurred_at, "occurred_at")
        _require_aware(self.received_at, "received_at")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
