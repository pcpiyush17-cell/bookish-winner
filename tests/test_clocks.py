from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_quant.clocks import SimulatedClock, SystemClock


def test_system_clock_returns_aware_utc_time() -> None:
    current = SystemClock().now()

    assert current.tzinfo is UTC


def test_simulated_clock_normalizes_and_advances() -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    clock = SimulatedClock(datetime(2026, 7, 28, 15, 30, tzinfo=india))

    assert clock.now() == datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    assert clock.advance(timedelta(minutes=5)) == datetime(2026, 7, 28, 10, 5, tzinfo=UTC)


def test_simulated_clock_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SimulatedClock(datetime(2026, 7, 28, 10, 0))


def test_simulated_clock_rejects_backwards_movement() -> None:
    start = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    clock = SimulatedClock(start)

    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="backwards"):
        clock.set(start - timedelta(seconds=1))


def test_simulated_clock_sets_equal_or_later_aware_time() -> None:
    start = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    clock = SimulatedClock(start)

    assert clock.set(start) == start
    assert clock.set(start + timedelta(seconds=1)) == start + timedelta(seconds=1)
