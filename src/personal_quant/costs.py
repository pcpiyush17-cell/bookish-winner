"""Versioned equity-delivery cost estimates using Decimal arithmetic."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

PAISA = Decimal("0.01")
BPS = Decimal("10000")


class CostError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ExecutionCostModel(Protocol):
    def bps(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class FixedBpsModel:
    value: Decimal

    def bps(self) -> Decimal:
        if self.value < 0:
            raise CostError("execution_model_invalid", "Basis points cannot be negative")
        return self.value


@dataclass(frozen=True, slots=True)
class HalfSpreadPlusBpsModel:
    quoted_spread_bps: Decimal
    additional_bps: Decimal = Decimal(0)

    def bps(self) -> Decimal:
        if self.quoted_spread_bps < 0 or self.additional_bps < 0:
            raise CostError("execution_model_invalid", "Basis points cannot be negative")
        return self.quoted_spread_bps / 2 + self.additional_bps


@dataclass(frozen=True, slots=True)
class VolatilityScaledModel:
    volatility_bps: Decimal
    multiplier: Decimal

    def bps(self) -> Decimal:
        if self.volatility_bps < 0 or self.multiplier < 0:
            raise CostError("execution_model_invalid", "Volatility assumptions cannot be negative")
        return self.volatility_bps * self.multiplier


class ChargeRates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    brokerage_rate: Decimal = Field(ge=0)
    stt_buy_rate: Decimal = Field(ge=0)
    stt_sell_rate: Decimal = Field(ge=0)
    exchange_transaction_rate: Decimal = Field(ge=0)
    sebi_turnover_rate: Decimal = Field(ge=0)
    gst_rate: Decimal = Field(ge=0)
    stamp_duty_buy_rate: Decimal = Field(ge=0)
    dp_charge_per_scrip_sell: Decimal = Field(ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def parse_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("rates must be quoted decimal strings")
        try:
            return Decimal(value)
        except InvalidOperation:
            return value


class FixedMonthlyCosts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kite_connect: Decimal = Field(ge=0)
    static_ip: Decimal = Field(ge=0)
    incremental_internet: Decimal = Field(ge=0)
    backup_storage: Decimal = Field(ge=0)
    paid_software: Decimal = Field(ge=0)
    purchased_data: Decimal = Field(ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def parse_decimal_string(cls, value: object) -> object:
        return ChargeRates.parse_decimal_string(value)

    def total(self) -> Decimal:
        return _money(sum((getattr(self, name) for name in type(self).model_fields), Decimal(0)))


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(ge=1)
    calculation_version: str = Field(min_length=1)
    rounding_rule: str = Field(pattern="^half_up_to_paise_per_component$")
    provider: str = Field(pattern="^zerodha$")
    exchange: str = Field(pattern="^NSE$")
    asset_class: str = Field(pattern="^equity_delivery$")
    verified_on: date
    source_urls: tuple[HttpUrl, ...] = Field(min_length=1)
    rates: ChargeRates
    fixed_monthly: FixedMonthlyCosts

    @field_validator("source_urls", mode="before")
    @classmethod
    def freeze_sources(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @classmethod
    def load(cls, path: Path) -> CostConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise CostError("cost_config_invalid", "Cost configuration cannot be loaded") from error


@dataclass(frozen=True, slots=True)
class DeliveryTrade:
    quantity: int
    buy_price: Decimal
    sell_price: Decimal
    spread_bps: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)
    impact_bps: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.buy_price <= 0 or self.sell_price <= 0:
            raise CostError("trade_invalid", "Quantity and prices must be positive")
        if min(self.spread_bps, self.slippage_bps, self.impact_bps) < 0:
            raise CostError("execution_cost_invalid", "Execution assumptions cannot be negative")

    def with_execution_models(
        self,
        *,
        spread: ExecutionCostModel,
        slippage: ExecutionCostModel,
        impact: ExecutionCostModel | None = None,
    ) -> DeliveryTrade:
        impact_model = impact or FixedBpsModel(Decimal(0))
        return replace(
            self,
            spread_bps=spread.bps(),
            slippage_bps=slippage.bps(),
            impact_bps=impact_model.bps(),
        )


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    calculation_version: str
    buy_turnover: Decimal
    sell_turnover: Decimal
    brokerage: Decimal
    stt: Decimal
    exchange_transaction_charge: Decimal
    sebi_turnover_charge: Decimal
    gst: Decimal
    stamp_duty: Decimal
    dp_charge: Decimal
    spread: Decimal
    slippage: Decimal
    impact: Decimal
    variable_total: Decimal
    gross_pnl: Decimal
    trading_net_pnl: Decimal
    scenario_multiplier: Decimal
    cost_kind: str = "estimate"


@dataclass(frozen=True, slots=True)
class BreakEvenReport:
    calculation_version: str
    break_even_rupees: Decimal
    required_monthly_gross_return_pct: Decimal
    api_cost_pct_of_capital: Decimal
    total_fixed_cost_pct_of_capital: Decimal
    capital_for_fixed_cost_below_1_pct: Decimal
    capital_for_fixed_cost_below_half_pct: Decimal
    capital_for_fixed_cost_below_quarter_pct: Decimal


@dataclass(frozen=True, slots=True)
class CostEngine:
    config: CostConfig

    def estimate(
        self, trade: DeliveryTrade, *, scenario_multiplier: Decimal = Decimal(1)
    ) -> CostBreakdown:
        if scenario_multiplier <= 0:
            raise CostError("scenario_invalid", "Scenario multiplier must be positive")
        rates = self.config.rates
        buy = _money(Decimal(trade.quantity) * trade.buy_price)
        sell = _money(Decimal(trade.quantity) * trade.sell_price)
        turnover = buy + sell
        brokerage = _money(turnover * rates.brokerage_rate)
        stt = _money(buy * rates.stt_buy_rate + sell * rates.stt_sell_rate)
        exchange = _money(turnover * rates.exchange_transaction_rate)
        sebi = _money(turnover * rates.sebi_turnover_rate)
        gst = _money((brokerage + exchange + sebi) * rates.gst_rate)
        stamp = _money(buy * rates.stamp_duty_buy_rate)
        dp = _money(rates.dp_charge_per_scrip_sell)
        spread = _money(turnover * trade.spread_bps / BPS * scenario_multiplier)
        slippage = _money(turnover * trade.slippage_bps / BPS * scenario_multiplier)
        impact = _money(turnover * trade.impact_bps / BPS * scenario_multiplier)
        total = _money(
            brokerage + stt + exchange + sebi + gst + stamp + dp + spread + slippage + impact
        )
        gross = _money(sell - buy)
        return CostBreakdown(
            self.config.calculation_version,
            buy,
            sell,
            brokerage,
            stt,
            exchange,
            sebi,
            gst,
            stamp,
            dp,
            spread,
            slippage,
            impact,
            total,
            gross,
            _money(gross - total),
            scenario_multiplier,
        )

    def stress_scenarios(self, trade: DeliveryTrade) -> dict[str, CostBreakdown]:
        return {
            "base": self.estimate(trade),
            "1.5x": self.estimate(trade, scenario_multiplier=Decimal("1.5")),
            "2.0x": self.estimate(trade, scenario_multiplier=Decimal("2.0")),
        }

    def allocated_fixed_cost(self, expected_monthly_trades: int) -> Decimal:
        if expected_monthly_trades <= 0:
            raise CostError("allocation_invalid", "Expected monthly trades must be positive")
        return _money(self.config.fixed_monthly.total() / expected_monthly_trades)

    def break_even(
        self,
        *,
        starting_capital: Decimal,
        expected_variable_costs: Decimal,
        target_net_profit: Decimal = Decimal(0),
    ) -> BreakEvenReport:
        if starting_capital <= 0 or expected_variable_costs < 0 or target_net_profit < 0:
            raise CostError(
                "break_even_input_invalid",
                "Capital must be positive; costs and target profit must be non-negative",
            )
        fixed = self.config.fixed_monthly.total()
        required = _money(fixed + expected_variable_costs + target_net_profit)
        return BreakEvenReport(
            self.config.calculation_version,
            required,
            _percent(required, starting_capital),
            _percent(self.config.fixed_monthly.kite_connect, starting_capital),
            _percent(fixed, starting_capital),
            _money(fixed / Decimal("0.01")),
            _money(fixed / Decimal("0.005")),
            _money(fixed / Decimal("0.0025")),
        )


def _money(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def _percent(value: Decimal, capital: Decimal) -> Decimal:
    return (value / capital * Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
