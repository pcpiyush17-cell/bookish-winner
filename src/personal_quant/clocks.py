"""Injected clock contracts for deterministic production and test logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Source of the current timezone-aware UTC time."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Wall clock used only through dependency injection."""

    def now(self) -> datetime:
        """Return the current wall-clock time in UTC."""
        return datetime.now(UTC)


@dataclass(slots=True)
class SimulatedClock:
    """Manually controlled monotonic clock for tests, replay, and backtests."""

    _current: datetime

    def __post_init__(self) -> None:
        _require_aware(self._current)
        self._current = self._current.astimezone(UTC)

    def now(self) -> datetime:
        """Return the simulated UTC time."""
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        """Move forward by a non-negative duration and return the new time."""
        if delta < timedelta(0):
            raise ValueError("SimulatedClock cannot move backwards")
        self._current += delta
        return self._current

    def set(self, value: datetime) -> datetime:
        """Move to an aware instant that is not earlier than the current time."""
        _require_aware(value)
        candidate = value.astimezone(UTC)
        if candidate < self._current:
            raise ValueError("SimulatedClock cannot move backwards")
        self._current = candidate
        return self._current


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock datetime must be timezone-aware")
