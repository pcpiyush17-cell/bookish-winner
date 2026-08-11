from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from personal_quant.market_calendar import CalendarError, MarketCalendar

CONFIG = Path("config/calendars/nse_equity_2026.yaml")


def test_calendar_handles_weekdays_weekends_holidays_and_pending_special() -> None:
    calendar = MarketCalendar.load(CONFIG)
    assert calendar.is_trading_day(date(2026, 7, 28))
    assert not calendar.is_trading_day(date(2026, 8, 1))
    assert not calendar.is_trading_day(date(2026, 10, 2))
    assert not calendar.is_trading_day(date(2026, 11, 8))
    assert calendar.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 5)


def test_market_and_strategy_windows_are_timezone_aware() -> None:
    calendar = MarketCalendar.load(CONFIG)
    assert calendar.is_market_open(datetime(2026, 7, 28, 4, 0, tzinfo=UTC))
    assert not calendar.is_strategy_window(datetime(2026, 7, 28, 3, 47, tzinfo=UTC))
    assert calendar.is_strategy_window(datetime(2026, 7, 28, 4, 0, tzinfo=UTC))
    with pytest.raises(CalendarError, match="timezone-aware"):
        calendar.is_market_open(datetime(2026, 7, 28, 10))


def test_calendar_applies_closing_auction_transition_from_august_third() -> None:
    calendar = MarketCalendar.load(CONFIG)
    july = calendar.session_times(date(2026, 7, 31))
    august = calendar.session_times(date(2026, 8, 3))

    assert july is not None
    assert august is not None
    assert july.market_close.isoformat() == "15:30:00"
    assert august.market_close.isoformat() == "15:15:00"


def test_calendar_rejects_dates_outside_version() -> None:
    calendar = MarketCalendar.load(CONFIG)
    with pytest.raises(CalendarError, match="outside"):
        calendar.is_trading_day(date(2027, 1, 1))


def test_invalid_calendar_fails_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "calendar.yaml"
    invalid.write_text("schema_version: 1\nunknown: true\n", encoding="utf-8")
    with pytest.raises(CalendarError, match="cannot be loaded"):
        MarketCalendar.load(invalid)
