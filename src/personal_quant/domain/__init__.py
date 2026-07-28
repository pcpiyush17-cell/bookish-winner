"""Shared domain contracts used by research and runtime components."""

from personal_quant.domain.events import CoreEventType, EventMeta
from personal_quant.domain.identifiers import (
    BrokerOrderId,
    ClientOrderId,
    ExchangeOrderId,
    FillId,
    IncidentId,
    InstrumentKey,
    InstrumentToken,
    OrderIntentId,
    RiskDecisionId,
    RunId,
    SessionId,
    SignalId,
    StrategyId,
    StrategyVersion,
)
from personal_quant.domain.money import Currency, Money

__all__ = [
    "BrokerOrderId",
    "ClientOrderId",
    "CoreEventType",
    "Currency",
    "EventMeta",
    "ExchangeOrderId",
    "FillId",
    "IncidentId",
    "InstrumentKey",
    "InstrumentToken",
    "Money",
    "OrderIntentId",
    "RiskDecisionId",
    "RunId",
    "SessionId",
    "SignalId",
    "StrategyId",
    "StrategyVersion",
]
