from datetime import UTC, datetime, timedelta

import pytest

from personal_quant.broker.contracts import BrokerError
from personal_quant.broker.rate_limit import BrokerRateLimiter
from personal_quant.clocks import SimulatedClock
from personal_quant.domain.identifiers import BrokerOrderId


def test_new_order_minute_limit_resets_after_window() -> None:
    clock = SimulatedClock(datetime(2026, 7, 28, 10, 0, tzinfo=UTC))
    limiter = BrokerRateLimiter(clock)
    limiter.acquire_new_order()
    limiter.acquire_new_order()

    with pytest.raises(BrokerError) as error:
        limiter.acquire_new_order()
    assert error.value.code == "new_order_rate_limit"

    clock.advance(timedelta(minutes=1, seconds=1))
    limiter.acquire_new_order()


def test_daily_and_modification_limits_are_independent() -> None:
    clock = SimulatedClock(datetime(2026, 7, 28, 10, 0, tzinfo=UTC))
    limiter = BrokerRateLimiter(
        clock,
        max_new_orders_per_minute=10,
        max_total_non_cancel_requests_per_day=3,
        max_modifications_per_order=2,
    )
    order_id = BrokerOrderId("order-1")
    limiter.acquire_new_order()
    limiter.acquire_modification(order_id)
    limiter.acquire_modification(order_id)

    with pytest.raises(BrokerError) as modification_error:
        limiter.acquire_modification(order_id)
    assert modification_error.value.code == "modification_limit"

    with pytest.raises(BrokerError) as daily_error:
        limiter.acquire_new_order()
    assert daily_error.value.code == "daily_order_rate_limit"

    limiter.record_cancellation()


def test_daily_limit_resets_on_india_trading_day() -> None:
    clock = SimulatedClock(datetime(2026, 7, 28, 18, 29, tzinfo=UTC))
    limiter = BrokerRateLimiter(
        clock, max_new_orders_per_minute=10, max_total_non_cancel_requests_per_day=1
    )
    limiter.acquire_new_order()
    clock.advance(timedelta(minutes=2))
    limiter.acquire_new_order()
