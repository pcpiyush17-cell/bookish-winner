"""Broker-independent strategy contracts and the first engineering baseline."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money

SIGNAL_NAMESPACE = UUID("4c9aa672-5309-4b4f-9c2f-ce048e5196df")


class StrategyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SignalDirection(StrEnum):
    LONG = "LONG"
    FLAT = "FLAT"


class SignalPurpose(StrEnum):
    ENTRY = "ENTRY"
    ADJUSTMENT = "ADJUSTMENT"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    instrument: InstrumentKey
    timestamp: datetime
    open: Money
    high: Money
    low: Money
    close: Money
    volume: int

    def __post_init__(self) -> None:
        _aware(self.timestamp)
        if (
            self.volume < 0
            or min(self.open.amount, self.high.amount, self.low.amount, self.close.amount) <= 0
        ):
            raise StrategyError("market_event_invalid", "Market event values are invalid")


@dataclass(frozen=True, slots=True)
class MarketState:
    closes: tuple[Money, ...]
    volumes: tuple[int, ...]
    regime_positive: bool


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    cash: Money
    positions: dict[InstrumentKey, int]
    average_entry: dict[InstrumentKey, Money]


@dataclass(frozen=True, slots=True)
class StrategyContext:
    mode: str
    started_at: datetime

    def __post_init__(self) -> None:
        _aware(self.started_at)
        if self.mode not in {"backtest", "paper", "replay"}:
            raise StrategyError("strategy_mode_invalid", "Strategy mode is unsupported")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    signal_id: UUID
    status: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: UUID
    strategy_id: str
    strategy_version: str
    instrument: InstrumentKey
    timestamp: datetime
    direction: SignalDirection
    strength: Decimal
    target_position: int
    expected_holding_bars: int
    invalidation_condition: str
    reason_codes: tuple[str, ...]
    feature_snapshot: dict[str, str]
    model_version: str
    expires_at: datetime
    purpose: SignalPurpose


class Strategy(Protocol):
    strategy_id: str
    version: str

    def required_instruments(self) -> set[InstrumentKey]: ...

    def required_features(self) -> set[str]: ...

    def on_start(self, context: StrategyContext) -> None: ...

    def on_market_event(
        self,
        event: MarketEvent,
        state: MarketState,
        portfolio: PortfolioSnapshot,
    ) -> list[Signal]: ...

    def on_order_event(self, event: OrderEvent) -> None: ...

    def on_stop(self, reason: str) -> None: ...


class BaselineMomentumConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(ge=1)
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    model_version: str = Field(min_length=1)
    universe: tuple[str, ...] = Field(min_length=1)
    fast_window: int = Field(ge=2)
    slow_window: int = Field(ge=3)
    volatility_lookback: int = Field(ge=2)
    max_volatility_pct: Decimal = Field(gt=0)
    minimum_average_traded_value_inr: Decimal = Field(gt=0)
    require_positive_regime: bool
    target_quantity: int = Field(gt=0)
    maximum_holding_bars: int = Field(gt=0)
    stop_loss_pct: Decimal = Field(gt=0, lt=100)
    expected_edge_bps: Decimal = Field(gt=0)
    estimated_cost_bps: Decimal = Field(ge=0)
    uncertainty_buffer_bps: Decimal = Field(ge=0)
    signal_expiry_minutes: int = Field(gt=0)

    @field_validator("universe", mode="before")
    @classmethod
    def freeze_universe(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator(
        "max_volatility_pct",
        "minimum_average_traded_value_inr",
        "stop_loss_pct",
        "expected_edge_bps",
        "estimated_cost_bps",
        "uncertainty_buffer_bps",
        mode="before",
    )
    @classmethod
    def decimal_strings(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("strategy decimals must be quoted strings")
        return Decimal(value)

    @model_validator(mode="after")
    def windows_and_edge(self) -> BaselineMomentumConfig:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        if self.volatility_lookback > self.slow_window:
            raise ValueError("volatility_lookback cannot exceed slow_window")
        if self.expected_edge_bps <= self.estimated_cost_bps + self.uncertainty_buffer_bps:
            raise ValueError("expected edge must exceed costs and uncertainty buffer")
        if len(self.universe) != len(set(self.universe)):
            raise ValueError("strategy universe must be unique")
        for item in self.universe:
            _instrument(item)
        return self

    @classmethod
    def load(cls, path: Path) -> BaselineMomentumConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise StrategyError(
                "strategy_config_invalid", "Strategy configuration is invalid"
            ) from error

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class BaselineMomentumStrategy:
    def __init__(self, config: BaselineMomentumConfig) -> None:
        self.config = config
        self.strategy_id = config.strategy_id
        self.version = config.version
        self._active = False
        self._holding_bars: dict[InstrumentKey, int] = defaultdict(int)

    def required_instruments(self) -> set[InstrumentKey]:
        return {InstrumentKey(item) for item in self.config.universe}

    def required_features(self) -> set[str]:
        return {
            f"sma_{self.config.fast_window}",
            f"sma_{self.config.slow_window}",
            f"volatility_{self.config.volatility_lookback}",
            "average_traded_value",
            "market_regime",
        }

    def on_start(self, context: StrategyContext) -> None:
        del context
        self._active = True
        self._holding_bars.clear()

    def on_market_event(
        self,
        event: MarketEvent,
        state: MarketState,
        portfolio: PortfolioSnapshot,
    ) -> list[Signal]:
        if not self._active:
            raise StrategyError("strategy_not_started", "Strategy must be started before events")
        if event.instrument not in self.required_instruments():
            return []
        quantity = portfolio.positions.get(event.instrument, 0)
        self._holding_bars[event.instrument] = (
            self._holding_bars[event.instrument] + 1 if quantity > 0 else 0
        )
        if len(state.closes) < self.config.slow_window:
            return []
        features = self._features(event, state)
        trend_positive = Decimal(features["fast_average"]) > Decimal(features["slow_average"])
        liquid = Decimal(features["average_traded_value"]) >= (
            self.config.minimum_average_traded_value_inr
        )
        volatility_safe = Decimal(features["volatility_pct"]) <= self.config.max_volatility_pct
        regime_safe = state.regime_positive or not self.config.require_positive_regime
        if quantity == 0:
            if portfolio.positions or not all(
                (trend_positive, liquid, volatility_safe, regime_safe)
            ):
                return []
            return [
                self._signal(
                    event,
                    SignalDirection.LONG,
                    self.config.target_quantity,
                    SignalPurpose.ENTRY,
                    ("trend_positive", "liquidity_pass", "volatility_pass", "regime_pass"),
                    features,
                )
            ]
        average_entry = portfolio.average_entry.get(event.instrument)
        stop = bool(
            average_entry
            and event.close.amount
            <= average_entry.amount * (Decimal(1) - self.config.stop_loss_pct / 100)
        )
        timed_out = self._holding_bars[event.instrument] >= self.config.maximum_holding_bars
        if not trend_positive or stop or timed_out:
            reasons = tuple(
                reason
                for condition, reason in (
                    (not trend_positive, "trend_reversal"),
                    (stop, "risk_stop"),
                    (timed_out, "time_stop"),
                )
                if condition
            )
            return [
                self._signal(
                    event,
                    SignalDirection.FLAT,
                    0,
                    SignalPurpose.EXIT,
                    reasons,
                    features,
                )
            ]
        return []

    def on_order_event(self, event: OrderEvent) -> None:
        _aware(event.occurred_at)

    def on_stop(self, reason: str) -> None:
        if not reason.strip():
            raise StrategyError("stop_reason_missing", "Strategy stop reason is required")
        self._active = False

    def _features(self, event: MarketEvent, state: MarketState) -> dict[str, str]:
        closes = [item.amount for item in state.closes]
        fast = _average(closes[-self.config.fast_window :])
        slow = _average(closes[-self.config.slow_window :])
        returns = [
            closes[index] / closes[index - 1] - 1
            for index in range(len(closes) - self.config.volatility_lookback, len(closes))
        ]
        volatility = _sample_std(returns) * 100
        traded_values = [
            state.closes[index].amount * state.volumes[index]
            for index in range(len(state.closes) - self.config.slow_window, len(state.closes))
        ]
        return {
            "close": str(event.close.amount),
            "fast_average": str(_decimal(fast)),
            "slow_average": str(_decimal(slow)),
            "volatility_pct": str(_decimal(volatility)),
            "average_traded_value": str(_decimal(_average(traded_values))),
            "regime_positive": str(state.regime_positive).lower(),
        }

    def _signal(
        self,
        event: MarketEvent,
        direction: SignalDirection,
        target: int,
        purpose: SignalPurpose,
        reasons: tuple[str, ...],
        features: dict[str, str],
    ) -> Signal:
        trend_gap = abs(Decimal(features["fast_average"]) / Decimal(features["slow_average"]) - 1)
        strength = min(Decimal(1), trend_gap * 100)
        identity = "|".join(
            (
                self.strategy_id,
                self.version,
                str(event.instrument),
                event.timestamp.astimezone(UTC).isoformat(),
                purpose.value,
            )
        )
        return Signal(
            uuid5(SIGNAL_NAMESPACE, identity),
            self.strategy_id,
            self.version,
            event.instrument,
            event.timestamp,
            direction,
            _decimal(strength),
            target,
            self.config.maximum_holding_bars,
            "trend reversal, time stop, or configured risk stop",
            reasons,
            features,
            self.config.model_version,
            event.timestamp + timedelta(minutes=self.config.signal_expiry_minutes),
            purpose,
        )


@dataclass(slots=True)
class StrategyRunner:
    strategy: Strategy
    mode: str
    regime_positive: bool = True
    _closes: dict[InstrumentKey, list[Money]] | None = None
    _volumes: dict[InstrumentKey, list[int]] | None = None

    def start(self, at: datetime) -> None:
        self._closes = defaultdict(list)
        self._volumes = defaultdict(list)
        self.strategy.on_start(StrategyContext(self.mode, at))

    def on_market_event(
        self, event: MarketEvent, portfolio: PortfolioSnapshot
    ) -> tuple[Signal, ...]:
        if self._closes is None or self._volumes is None:
            raise StrategyError("runner_not_started", "Strategy runner has not started")
        self._closes[event.instrument].append(event.close)
        self._volumes[event.instrument].append(event.volume)
        state = MarketState(
            tuple(self._closes[event.instrument]),
            tuple(self._volumes[event.instrument]),
            self.regime_positive,
        )
        return tuple(self.strategy.on_market_event(event, state, portfolio))

    def stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    starting_capital: Money
    ending_capital: Money
    return_pct: Decimal
    quantity: int
    entry_price: Money
    exit_price: Money


def buy_and_hold_benchmark(
    events: tuple[MarketEvent, ...], starting_capital: Money
) -> BenchmarkResult:
    if not events or starting_capital.amount <= 0:
        raise StrategyError("benchmark_input_invalid", "Benchmark inputs are invalid")
    ordered = tuple(sorted(events, key=lambda item: item.timestamp))
    if len({item.instrument for item in ordered}) != 1:
        raise StrategyError("benchmark_instrument_invalid", "Benchmark requires one instrument")
    entry, exit_price = ordered[0].open, ordered[-1].close
    quantity = int(starting_capital.amount // entry.amount)
    ending = Money(starting_capital.amount - entry.amount * quantity + exit_price.amount * quantity)
    return BenchmarkResult(
        starting_capital,
        ending,
        _decimal((ending.amount / starting_capital.amount - 1) * 100),
        quantity,
        entry,
        exit_price,
    )


def strategy_manifest(config: BaselineMomentumConfig) -> dict[str, object]:
    return {
        "strategy_id": config.strategy_id,
        "version": config.version,
        "model_version": config.model_version,
        "config_hash": config.fingerprint(),
        "required_features": sorted(BaselineMomentumStrategy(config).required_features()),
        "required_instruments": sorted(config.universe),
    }


def _instrument(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2 or parts[0] != "NSE" or not parts[1]:
        raise ValueError("strategy instruments must use NSE:TRADINGSYMBOL")


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _sample_std(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    mean = _average(values)
    return (sum(((item - mean) ** 2 for item in values), Decimal(0)) / (len(values) - 1)).sqrt()


def _decimal(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StrategyError("strategy_time_naive", "Strategy timestamps must be timezone-aware")
