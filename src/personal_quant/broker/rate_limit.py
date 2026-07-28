"""Conservative in-memory broker request limits driven by an injected clock."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from personal_quant.clocks import Clock
from personal_quant.domain.identifiers import BrokerOrderId

from .contracts import BrokerError

_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")


@dataclass(slots=True)
class BrokerRateLimiter:
    """Apply V1 limits while always leaving cancellation available for safety."""

    clock: Clock
    max_new_orders_per_minute: int = 2
    max_total_non_cancel_requests_per_day: int = 10
    max_modifications_per_order: int = 2
    _new_orders: deque[datetime] = field(default_factory=deque, init=False)
    _daily_requests: dict[str, int] = field(default_factory=dict, init=False)
    _modifications: dict[BrokerOrderId, int] = field(
        default_factory=lambda: defaultdict(int), init=False
    )
    _lock: Lock = field(default_factory=Lock, init=False)

    def acquire_new_order(self) -> None:
        with self._lock:
            now = self.clock.now()
            cutoff = now - timedelta(minutes=1)
            while self._new_orders and self._new_orders[0] <= cutoff:
                self._new_orders.popleft()
            if len(self._new_orders) >= self.max_new_orders_per_minute:
                raise BrokerError("new_order_rate_limit", "New-order minute limit reached")
            self._acquire_daily(now)
            self._new_orders.append(now)

    def acquire_modification(self, order_id: BrokerOrderId) -> None:
        with self._lock:
            if self._modifications[order_id] >= self.max_modifications_per_order:
                raise BrokerError("modification_limit", "Order modification limit reached")
            now = self.clock.now()
            self._acquire_daily(now)
            self._modifications[order_id] += 1

    def record_cancellation(self) -> None:
        """Record no blocking limit: cancelling risk must remain possible."""

    def _acquire_daily(self, now: datetime) -> None:
        day = now.astimezone(_MARKET_TIMEZONE).date().isoformat()
        count = self._daily_requests.get(day, 0)
        if count >= self.max_total_non_cancel_requests_per_day:
            raise BrokerError("daily_order_rate_limit", "Daily order-request limit reached")
        self._daily_requests = {day: count + 1}
