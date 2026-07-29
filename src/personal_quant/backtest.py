"""Deterministic event-driven backtesting with explicit no-lookahead execution."""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

from personal_quant.broker.contracts import OrderSide
from personal_quant.clocks import SimulatedClock
from personal_quant.costs import CostEngine, DeliveryTrade
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.domain.money import Money

RUN_NAMESPACE = UUID("7ee75fe3-1ac2-41a7-8c62-59110a24df26")
BPS = Decimal("10000")


class BacktestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BacktestBar:
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
            raise BacktestError(
                "bar_invalid", "Bar prices must be positive and volume non-negative"
            )
        if (
            self.high.amount < max(self.open.amount, self.close.amount)
            or self.low.amount > min(self.open.amount, self.close.amount)
            or self.low.amount > self.high.amount
        ):
            raise BacktestError("bar_ohlc_invalid", "Bar OHLC bounds are inconsistent")


@dataclass(frozen=True, slots=True)
class BacktestOrder:
    order_id: UUID
    instrument: InstrumentKey
    side: OrderSide
    quantity: int
    submitted_at: datetime
    limit_price: Money | None = None

    def __post_init__(self) -> None:
        _aware(self.submitted_at)
        if self.quantity <= 0:
            raise BacktestError("order_quantity_invalid", "Order quantity must be positive")
        if self.limit_price is not None and self.limit_price.amount <= 0:
            raise BacktestError("order_limit_invalid", "Limit price must be positive")


@dataclass(frozen=True, slots=True)
class BacktestFill:
    order_id: UUID
    instrument: InstrumentKey
    side: OrderSide
    quantity: int
    price: Money
    filled_at: datetime
    slippage_bps: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioView:
    cash: Money
    positions: dict[InstrumentKey, int]
    equity: Money


SignalFunction = Callable[[BacktestBar, PortfolioView], Sequence[BacktestOrder]]


class SlippageModel(Protocol):
    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class FixedSlippage:
    value_bps: Decimal

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        del order, bar
        return _non_negative(self.value_bps)


@dataclass(frozen=True, slots=True)
class HalfSpreadSlippage:
    spread_bps: Decimal
    additional_bps: Decimal = Decimal(0)

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        del order, bar
        return _non_negative(self.spread_bps) / 2 + _non_negative(self.additional_bps)


@dataclass(frozen=True, slots=True)
class VolatilitySlippage:
    volatility_bps: Decimal
    multiplier: Decimal = Decimal(1)

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        del order, bar
        return _non_negative(self.volatility_bps) * _non_negative(self.multiplier)


@dataclass(frozen=True, slots=True)
class ParticipationSlippage:
    base_bps: Decimal
    bps_per_participation_pct: Decimal

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        if bar.volume <= 0:
            raise BacktestError("bar_liquidity_missing", "Participation requires positive volume")
        participation_pct = Decimal(order.quantity) / Decimal(bar.volume) * 100
        return _non_negative(self.base_bps) + participation_pct * _non_negative(
            self.bps_per_participation_pct
        )


@dataclass(frozen=True, slots=True)
class SymbolSlippage:
    values_bps: dict[InstrumentKey, Decimal]
    default_bps: Decimal = Decimal(0)

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        del bar
        return _non_negative(self.values_bps.get(order.instrument, self.default_bps))


@dataclass(frozen=True, slots=True)
class TimeOfDaySlippage:
    base_bps: Decimal
    multipliers: dict[int, Decimal]

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        del order
        return _non_negative(self.base_bps) * _non_negative(
            self.multipliers.get(bar.timestamp.hour, Decimal(1))
        )


@dataclass(frozen=True, slots=True)
class CompositeSlippage:
    models: tuple[SlippageModel, ...]

    def bps(self, order: BacktestOrder, bar: BacktestBar) -> Decimal:
        return sum((model.bps(order, bar) for model in self.models), Decimal(0))


class FillModel(Protocol):
    def fill(
        self, order: BacktestOrder, bar: BacktestBar, slippage: SlippageModel
    ) -> BacktestFill | None: ...


@dataclass(frozen=True, slots=True)
class NextBarOpenFill:
    def fill(
        self, order: BacktestOrder, bar: BacktestBar, slippage: SlippageModel
    ) -> BacktestFill | None:
        if order.instrument != bar.instrument or bar.timestamp <= order.submitted_at:
            return None
        bps = slippage.bps(order, bar)
        direction = Decimal(1) if order.side is OrderSide.BUY else Decimal(-1)
        price = Money(bar.open.amount * (Decimal(1) + direction * bps / BPS))
        return BacktestFill(
            order.order_id,
            order.instrument,
            order.side,
            order.quantity,
            price,
            bar.timestamp,
            bps,
        )


@dataclass(frozen=True, slots=True)
class LimitTouchFill:
    def fill(
        self, order: BacktestOrder, bar: BacktestBar, slippage: SlippageModel
    ) -> BacktestFill | None:
        if order.limit_price is None:
            raise BacktestError("limit_required", "Limit-touch model requires a limit order")
        if order.instrument != bar.instrument or bar.timestamp <= order.submitted_at:
            return None
        touched = (order.side is OrderSide.BUY and bar.low.amount <= order.limit_price.amount) or (
            order.side is OrderSide.SELL and bar.high.amount >= order.limit_price.amount
        )
        if not touched:
            return None
        bps = slippage.bps(order, bar)
        direction = Decimal(1) if order.side is OrderSide.BUY else Decimal(-1)
        slipped = Money(order.limit_price.amount * (Decimal(1) + direction * bps / BPS))
        price = (
            Money(min(slipped.amount, order.limit_price.amount))
            if order.side is OrderSide.BUY
            else Money(max(slipped.amount, order.limit_price.amount))
        )
        return BacktestFill(
            order.order_id,
            order.instrument,
            order.side,
            order.quantity,
            price,
            bar.timestamp,
            bps,
        )


class EventPriority(IntEnum):
    MARKET = 0
    ORDER = 1


@dataclass(order=True, frozen=True, slots=True)
class Event:
    timestamp: datetime
    priority: int
    sequence: int
    kind: str = field(compare=False)
    payload: object = field(compare=False)


@dataclass(slots=True)
class EventQueue:
    _events: list[Event] = field(default_factory=list)
    _sequence: int = 0

    def put(self, timestamp: datetime, priority: EventPriority, kind: str, payload: object) -> None:
        _aware(timestamp)
        heapq.heappush(
            self._events,
            Event(timestamp.astimezone(UTC), int(priority), self._sequence, kind, payload),
        )
        self._sequence += 1

    def pop(self) -> Event:
        if not self._events:
            raise BacktestError("event_queue_empty", "Event queue is empty")
        return heapq.heappop(self._events)

    def __bool__(self) -> bool:
        return bool(self._events)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    instrument: InstrumentKey
    quantity: int
    bought_at: datetime
    sold_at: datetime
    buy_price: Money
    sell_price: Money
    gross_pnl: Money


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    cash: Money
    market_value: Money
    equity: Money
    exposure_pct: Decimal


@dataclass(frozen=True, slots=True)
class PositionPoint:
    timestamp: datetime
    instrument: InstrumentKey
    quantity: int
    mark_price: Money
    market_value: Money


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    starting_capital: Decimal
    ending_capital: Decimal
    gross_return_pct: Decimal
    trading_net_return_pct: Decimal
    operating_net_return_pct: Decimal
    maximum_drawdown_pct: Decimal
    drawdown_duration_bars: int
    volatility_pct: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    hit_rate_pct: Decimal
    average_winner: Decimal
    average_loser: Decimal
    payoff_ratio: Decimal | None
    profit_factor: Decimal | None
    turnover: Decimal
    average_holding_hours: Decimal
    exposure_pct: Decimal
    trade_count: int
    pnl_concentration_pct: Decimal
    cost_to_gross_profit_pct: Decimal | None
    worst_day: Decimal
    worst_week: Decimal
    worst_month: Decimal
    benchmark_return_pct: Decimal | None
    annualized_metrics_headlined: bool = False


@dataclass(frozen=True, slots=True)
class CostScenario:
    multiplier: Decimal
    variable_costs: Decimal
    ending_capital: Decimal
    trading_net_return_pct: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    fills: tuple[BacktestFill, ...]
    closed_trades: tuple[ClosedTrade, ...]
    equity: tuple[EquityPoint, ...]
    positions: tuple[PositionPoint, ...]
    metrics: BacktestMetrics
    cost_scenarios: dict[str, CostScenario]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_capital: Money
    fixed_operating_costs: Money = field(default_factory=lambda: Money(Decimal(0)))
    benchmark_return_pct: Decimal | None = None

    def __post_init__(self) -> None:
        if self.starting_capital.amount <= 0 or self.fixed_operating_costs.amount < 0:
            raise BacktestError(
                "capital_invalid", "Capital must be positive and costs non-negative"
            )


@dataclass(frozen=True, slots=True)
class EventDrivenBacktester:
    config: BacktestConfig
    fill_model: FillModel
    slippage_model: SlippageModel
    cost_engine: CostEngine

    def run(
        self,
        bars: Iterable[BacktestBar],
        signal: SignalFunction,
        *,
        run_config: dict[str, object],
        data_checksums: Sequence[str],
    ) -> BacktestResult:
        ordered = tuple(sorted(bars, key=lambda item: (item.timestamp, str(item.instrument))))
        _validate_bars(ordered)
        fingerprint = _fingerprint(run_config, data_checksums, ordered)
        clock = SimulatedClock(ordered[0].timestamp)
        queue = EventQueue()
        for bar in ordered:
            queue.put(bar.timestamp, EventPriority.MARKET, "market", bar)
        pending: list[BacktestOrder] = []
        fills: list[BacktestFill] = []
        closed: list[ClosedTrade] = []
        lots: dict[InstrumentKey, list[tuple[int, Money, datetime]]] = {}
        cash = self.config.starting_capital
        latest: dict[InstrumentKey, Money] = {}
        equity: list[EquityPoint] = []
        position_points: list[PositionPoint] = []
        seen_orders: set[UUID] = set()
        while queue:
            event = queue.pop()
            clock.set(event.timestamp)
            bar = _require_bar(event.payload)
            latest[bar.instrument] = bar.close
            remaining: list[BacktestOrder] = []
            for order in pending:
                fill = self.fill_model.fill(order, bar, self.slippage_model)
                if fill is None:
                    remaining.append(order)
                    continue
                cash = _apply_fill(fill, cash, lots, closed)
                fills.append(fill)
            pending = remaining
            positions = {key: sum(item[0] for item in values) for key, values in lots.items()}
            market_value = Money(
                sum(
                    (latest[key].amount * quantity for key, quantity in positions.items()),
                    Decimal(0),
                )
            )
            total = cash + market_value
            exposure = market_value.amount / total.amount * 100 if total.amount else Decimal(0)
            equity.append(EquityPoint(bar.timestamp, cash, market_value, total, exposure))
            position_points.extend(
                PositionPoint(
                    bar.timestamp,
                    key,
                    quantity,
                    latest[key],
                    latest[key].multiply(quantity),
                )
                for key, quantity in sorted(positions.items(), key=lambda item: str(item[0]))
                if quantity
            )
            view = PortfolioView(cash, positions, total)
            for order in signal(bar, view):
                if order.order_id in seen_orders:
                    raise BacktestError(
                        "order_id_duplicate", "Strategy emitted a duplicate order ID"
                    )
                if order.submitted_at != bar.timestamp:
                    raise BacktestError(
                        "signal_time_invalid",
                        "Orders must be timestamped at the observed bar close",
                    )
                seen_orders.add(order.order_id)
                pending.append(order)
        scenarios = self._cost_scenarios(closed, fills, equity[-1].equity)
        metrics = _metrics(self.config, equity, closed, scenarios["base"])
        warnings = (
            ("annualized_metrics_suppressed_short_sample",)
            if len({point.timestamp.date() for point in equity}) < 252
            else ()
        )
        return BacktestResult(
            str(uuid5(RUN_NAMESPACE, fingerprint)),
            tuple(fills),
            tuple(closed),
            tuple(equity),
            tuple(position_points),
            metrics,
            scenarios,
            warnings,
        )

    def _cost_scenarios(
        self,
        trades: Sequence[ClosedTrade],
        fills: Sequence[BacktestFill],
        gross_ending_equity: Money,
    ) -> dict[str, CostScenario]:
        result: dict[str, CostScenario] = {}
        realized_execution_cost = sum(
            (fill.price.amount * fill.quantity * fill.slippage_bps / BPS for fill in fills),
            Decimal(0),
        )
        for name, multiplier in (
            ("base", Decimal(1)),
            ("1.5x", Decimal("1.5")),
            ("2.0x", Decimal("2.0")),
        ):
            statutory_costs = sum(
                (
                    self.cost_engine.estimate(
                        DeliveryTrade(item.quantity, item.buy_price.amount, item.sell_price.amount),
                        scenario_multiplier=multiplier,
                    ).variable_total
                    for item in trades
                ),
                Decimal(0),
            )
            costs = statutory_costs + realized_execution_cost * (multiplier - Decimal(1))
            ending = gross_ending_equity.amount - costs
            result[name] = CostScenario(
                multiplier,
                _money(costs),
                _money(ending),
                _percent(
                    ending - self.config.starting_capital.amount,
                    self.config.starting_capital.amount,
                ),
            )
        return result


@dataclass(frozen=True, slots=True)
class ArtifactWriter:
    root: Path

    def write(
        self,
        result: BacktestResult,
        *,
        run_config: dict[str, object],
        data_manifest: dict[str, object],
        strategy_manifest: dict[str, object],
        git_commit: str,
        environment_lock_hash: str,
    ) -> Path:
        target = self.root / result.run_id
        if target.exists():
            raise BacktestError("artifact_exists", "Backtest artefacts are immutable")
        target.mkdir(parents=True)
        _json(target / "config.json", run_config)
        _json(target / "data_manifest.json", data_manifest)
        _json(target / "strategy_manifest.json", strategy_manifest)
        _json(target / "metrics.json", asdict(result.metrics))
        _json(
            target / "cost_stress.json",
            {name: asdict(value) for name, value in result.cost_scenarios.items()},
        )
        _json(target / "warnings.json", list(result.warnings))
        _csv(target / "fills.csv", result.fills)
        _csv(target / "equity.csv", result.equity)
        _csv(target / "positions.csv", result.positions)
        _csv(target / "trades.csv", result.closed_trades)
        (target / "equity.svg").write_text(_equity_svg(result.equity), encoding="utf-8")
        (target / "run.log").write_text(
            f"run_id={result.run_id}\nstatus=complete\n", encoding="utf-8"
        )
        manifest = {
            "schema_version": 1,
            "run_id": result.run_id,
            "git_commit": git_commit,
            "environment_lock_hash": environment_lock_hash,
            "files": {
                path.name: _checksum(path)
                for path in sorted(target.iterdir())
                if path.name != "manifest.json"
            },
        }
        _json(target / "manifest.json", manifest)
        return target


def deterministic_order_id(strategy: str, bar: BacktestBar, sequence: int) -> UUID:
    return uuid5(
        RUN_NAMESPACE,
        f"{strategy}|{bar.instrument}|{bar.timestamp.astimezone(UTC).isoformat()}|{sequence}",
    )


def _apply_fill(
    fill: BacktestFill,
    cash: Money,
    lots: dict[InstrumentKey, list[tuple[int, Money, datetime]]],
    closed: list[ClosedTrade],
) -> Money:
    value = fill.price.multiply(fill.quantity)
    instrument_lots = lots.setdefault(fill.instrument, [])
    if fill.side is OrderSide.BUY:
        instrument_lots.append((fill.quantity, fill.price, fill.filled_at))
        return cash - value
    available = sum(item[0] for item in instrument_lots)
    if fill.quantity > available:
        raise BacktestError("short_sale_rejected", "Backtester does not permit short positions")
    remaining = fill.quantity
    while remaining:
        quantity, price, bought_at = instrument_lots[0]
        matched = min(quantity, remaining)
        closed.append(
            ClosedTrade(
                fill.instrument,
                matched,
                bought_at,
                fill.filled_at,
                price,
                fill.price,
                Money((fill.price.amount - price.amount) * matched),
            )
        )
        if matched == quantity:
            instrument_lots.pop(0)
        else:
            instrument_lots[0] = (quantity - matched, price, bought_at)
        remaining -= matched
    return cash + value


def _metrics(
    config: BacktestConfig,
    equity: Sequence[EquityPoint],
    trades: Sequence[ClosedTrade],
    base: CostScenario,
) -> BacktestMetrics:
    starting = config.starting_capital.amount
    gross_ending = equity[-1].equity.amount
    operating_ending = base.ending_capital - config.fixed_operating_costs.amount
    values = [item.equity.amount for item in equity]
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
    maximum_drawdown, duration = _drawdown(values)
    volatility = _sample_std(returns)
    downside = _sample_std([min(item, Decimal(0)) for item in returns])
    mean_return = sum(returns, Decimal(0)) / len(returns) if returns else Decimal(0)
    winners = [item.gross_pnl.amount for item in trades if item.gross_pnl.amount > 0]
    losers = [item.gross_pnl.amount for item in trades if item.gross_pnl.amount < 0]
    gross_profit = sum(winners, Decimal(0))
    gross_loss = abs(sum(losers, Decimal(0)))
    pnls = [abs(item.gross_pnl.amount) for item in trades]
    grouped_day = _period_returns(equity, lambda value: value.date())
    grouped_week = _period_returns(equity, lambda value: value.date().isocalendar()[:2])
    grouped_month = _period_returns(equity, lambda value: (value.year, value.month))
    turnover = sum(
        (
            Decimal(item.quantity) * (item.buy_price.amount + item.sell_price.amount)
            for item in trades
        ),
        Decimal(0),
    )
    exposure = sum((item.exposure_pct for item in equity), Decimal(0)) / len(equity)
    holding = sum(
        (Decimal(str((item.sold_at - item.bought_at).total_seconds())) / 3600 for item in trades),
        Decimal(0),
    )
    return BacktestMetrics(
        _money(starting),
        _money(base.ending_capital),
        _percent(gross_ending - starting, starting),
        _percent(base.ending_capital - starting, starting),
        _percent(operating_ending - starting, starting),
        _percent(maximum_drawdown, Decimal(1)),
        duration,
        _optional_percent(volatility),
        _ratio(mean_return, volatility),
        _ratio(mean_return, downside),
        _ratio((gross_ending / starting - 1), maximum_drawdown),
        _percent(Decimal(len(winners)), Decimal(len(trades))) if trades else Decimal(0),
        _average(winners),
        _average(losers),
        _ratio(_average(winners), abs(_average(losers))),
        _ratio(gross_profit, gross_loss),
        _money(turnover),
        _money(holding / len(trades)) if trades else Decimal(0),
        _money(exposure),
        len(trades),
        _percent(max(pnls, default=Decimal(0)), sum(pnls, Decimal(0))) if pnls else Decimal(0),
        _percent(base.variable_costs, gross_profit) if gross_profit else None,
        min(grouped_day, default=Decimal(0)),
        min(grouped_week, default=Decimal(0)),
        min(grouped_month, default=Decimal(0)),
        config.benchmark_return_pct,
    )


def _drawdown(values: Sequence[Decimal]) -> tuple[Decimal, int]:
    peak = values[0]
    worst = Decimal(0)
    current_duration = 0
    longest = 0
    for value in values:
        if value >= peak:
            peak = value
            current_duration = 0
        else:
            current_duration += 1
            longest = max(longest, current_duration)
            worst = max(worst, (peak - value) / peak)
    return worst, longest


def _sample_std(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values, Decimal(0)) / len(values)
    variance = sum(((item - mean) ** 2 for item in values), Decimal(0)) / (len(values) - 1)
    return variance.sqrt()


def _period_returns(
    equity: Sequence[EquityPoint], key: Callable[[datetime], object]
) -> list[Decimal]:
    periods: dict[object, tuple[Decimal, Decimal]] = {}
    for point in equity:
        period = key(point.timestamp)
        if period not in periods:
            periods[period] = (point.equity.amount, point.equity.amount)
        else:
            periods[period] = (periods[period][0], point.equity.amount)
    return [end / start - 1 for start, end in periods.values() if start]


def _validate_bars(bars: Sequence[BacktestBar]) -> None:
    if not bars:
        raise BacktestError("bars_empty", "Backtest requires at least one bar")
    keys = [(item.instrument, item.timestamp) for item in bars]
    if len(keys) != len(set(keys)):
        raise BacktestError("bars_duplicate", "Backtest bars contain duplicate keys")


def _fingerprint(
    config: dict[str, object], checksums: Sequence[str], bars: Sequence[BacktestBar]
) -> str:
    payload = {
        "config": config,
        "data_checksums": sorted(checksums),
        "bars": [
            [
                str(item.instrument),
                item.timestamp.astimezone(UTC).isoformat(),
                str(item.open.amount),
                str(item.high.amount),
                str(item.low.amount),
                str(item.close.amount),
                item.volume,
            ]
            for item in bars
        ],
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _require_bar(value: object) -> BacktestBar:
    if not isinstance(value, BacktestBar):
        raise BacktestError("event_payload_invalid", "Market event payload is not a bar")
    return value


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, default=_json_default, sort_keys=True, indent=2) + "\n")


def _csv(path: Path, rows: Sequence[object]) -> None:
    materialized = [asdict(row) for row in rows]  # type: ignore[call-overload]
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not materialized:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=list(materialized[0]))
        writer.writeheader()
        for row in materialized:
            writer.writerow({key: _flat(value) for key, value in row.items()})


def _flat(value: object) -> object:
    if isinstance(value, dict):
        return _canonical(value)
    if isinstance(value, Money):
        return str(value.amount)
    if isinstance(value, (datetime, UUID, StrEnum, Decimal)):
        return str(value)
    return value


def _json_default(value: object) -> object:
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency.value}
    if isinstance(value, (datetime, date)):
        return (
            value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value.isoformat()
        )
    if isinstance(value, (UUID, StrEnum, Decimal)):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _canonical(value: object) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _equity_svg(points: Sequence[EquityPoint]) -> str:
    values = [point.equity.amount for point in points]
    low, high = min(values), max(values)
    span = high - low or Decimal(1)
    width, height = 640, 240
    coordinates = []
    for index, value in enumerate(values):
        x = Decimal(index) * (width - 20) / max(len(values) - 1, 1) + 10
        y = Decimal(height - 10) - (value - low) / span * (height - 20)
        coordinates.append(f"{x.quantize(Decimal('0.01'))},{y.quantize(Decimal('0.01'))}")
    polyline = " ".join(coordinates)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="240" '
        'viewBox="0 0 640 240">\n'
        '  <rect width="640" height="240" fill="white"/>\n'
        f'  <polyline points="{polyline}" fill="none" stroke="#175CD3" stroke-width="2"/>\n'
        "</svg>\n"
    )


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BacktestError("time_naive", "Backtest timestamps must be timezone-aware")


def _non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise BacktestError("slippage_invalid", "Slippage assumptions cannot be negative")
    return value


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise BacktestError("metric_denominator_zero", "Metric denominator cannot be zero")
    return (numerator / denominator * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _optional_percent(value: Decimal | None) -> Decimal | None:
    return _percent(value, Decimal(1)) if value is not None else None


def _average(values: Sequence[Decimal]) -> Decimal:
    return _money(sum(values, Decimal(0)) / len(values)) if values else Decimal(0)


def _ratio(numerator: Decimal, denominator: Decimal | None) -> Decimal | None:
    if denominator in {None, Decimal(0)}:
        return None
    assert denominator is not None
    return (numerator / denominator).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
