"""Mode adapters that keep strategy logic independent from execution implementations."""

from __future__ import annotations

from dataclasses import dataclass

from personal_quant.backtest import BacktestBar, BacktestOrder, PortfolioView
from personal_quant.broker.contracts import OrderSide
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money
from personal_quant.paper import MarketBar
from personal_quant.strategy import (
    MarketEvent,
    PortfolioSnapshot,
    Signal,
    SignalDirection,
    StrategyRunner,
)


@dataclass(slots=True)
class BacktestStrategyAdapter:
    runner: StrategyRunner
    _started: bool = False

    def __call__(self, bar: BacktestBar, portfolio: PortfolioView) -> tuple[BacktestOrder, ...]:
        if not self._started:
            self.runner.start(bar.timestamp)
            self._started = True
        snapshot = PortfolioSnapshot(portfolio.cash, dict(portfolio.positions), {})
        signals = self.runner.on_market_event(_from_backtest(bar), snapshot)
        orders: list[BacktestOrder] = []
        for signal in signals:
            current = portfolio.positions.get(signal.instrument, 0)
            difference = signal.target_position - current
            if difference == 0:
                continue
            orders.append(
                BacktestOrder(
                    signal.signal_id,
                    signal.instrument,
                    OrderSide.BUY if difference > 0 else OrderSide.SELL,
                    abs(difference),
                    signal.timestamp,
                )
            )
        return tuple(orders)


@dataclass(slots=True)
class PaperStrategyAdapter:
    runner: StrategyRunner
    _started: bool = False

    def on_bar(
        self,
        bar: MarketBar,
        *,
        cash: Money,
        positions: dict[InstrumentKey, int],
        average_entry: dict[InstrumentKey, Money] | None = None,
    ) -> tuple[Signal, ...]:
        if not self._started:
            self.runner.start(bar.timestamp)
            self._started = True
        portfolio = PortfolioSnapshot(cash, dict(positions), dict(average_entry or {}))
        return self.runner.on_market_event(_from_paper(bar), portfolio)


def _from_backtest(bar: BacktestBar) -> MarketEvent:
    return MarketEvent(
        bar.instrument,
        bar.timestamp,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
    )


def _from_paper(bar: MarketBar) -> MarketEvent:
    return MarketEvent(
        bar.instrument,
        bar.timestamp,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
    )


def desired_side(signal: Signal, current_quantity: int) -> OrderSide | None:
    difference = signal.target_position - current_quantity
    if difference > 0 and signal.direction is SignalDirection.LONG:
        return OrderSide.BUY
    if difference < 0 and signal.direction is SignalDirection.FLAT:
        return OrderSide.SELL
    return None
