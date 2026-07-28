"""Strongly typed identifiers for domain boundaries."""

from typing import NewType
from uuid import UUID

StrategyId = NewType("StrategyId", str)
StrategyVersion = NewType("StrategyVersion", str)
SignalId = NewType("SignalId", UUID)
OrderIntentId = NewType("OrderIntentId", UUID)
ClientOrderId = NewType("ClientOrderId", str)
BrokerOrderId = NewType("BrokerOrderId", str)
ExchangeOrderId = NewType("ExchangeOrderId", str)
FillId = NewType("FillId", str)
InstrumentKey = NewType("InstrumentKey", str)
InstrumentToken = NewType("InstrumentToken", int)
RunId = NewType("RunId", UUID)
SessionId = NewType("SessionId", UUID)
RiskDecisionId = NewType("RiskDecisionId", UUID)
IncidentId = NewType("IncidentId", UUID)
