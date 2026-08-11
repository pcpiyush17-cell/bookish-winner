"""Versioned, source-backed exchange calendar with explicit special sessions."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class CalendarError(ValueError):
    """A calendar configuration or lookup failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SessionTimes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pre_open_start: time
    market_open: time
    strategy_start: time
    strategy_end: time
    market_close: time
    post_close_end: time

    @field_validator("*", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return time.fromisoformat(value)
            except ValueError:
                return value
        return value

    @model_validator(mode="after")
    def ordered(self) -> SessionTimes:
        values = (
            self.pre_open_start,
            self.market_open,
            self.strategy_start,
            self.strategy_end,
            self.market_close,
            self.post_close_end,
        )
        if list(values) != sorted(values):
            raise ValueError("session times must be ordered")
        return self


class SpecialSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_date: date
    name: str = Field(min_length=1)
    status: str = Field(pattern="^(confirmed|pending_timing)$")
    times: SessionTimes | None = None

    @model_validator(mode="after")
    def confirmed_has_times(self) -> SpecialSession:
        if (self.status == "confirmed") != (self.times is not None):
            raise ValueError("only confirmed special sessions must provide times")
        return self


class SessionChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    effective_from: date
    name: str = Field(min_length=1)
    times: SessionTimes


class CalendarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(ge=1)
    calendar_id: str = Field(min_length=1)
    exchange: str = Field(pattern="^NSE$")
    segment: str = Field(pattern="^equity$")
    timezone: str
    year: int = Field(ge=2000)
    verified_on: date
    source_urls: tuple[HttpUrl, ...] = Field(min_length=1)
    regular_session: SessionTimes
    holidays: tuple[date, ...]
    special_sessions: tuple[SpecialSession, ...] = ()
    session_changes: tuple[SessionChange, ...] = ()

    @field_validator(
        "source_urls", "holidays", "special_sessions", "session_changes", mode="before"
    )
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def internally_consistent(self) -> CalendarConfig:
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise ValueError("timezone must be an IANA timezone") from error
        dates = [
            *self.holidays,
            *(item.session_date for item in self.special_sessions),
            *(item.effective_from for item in self.session_changes),
        ]
        if any(item.year != self.year for item in dates):
            raise ValueError("all dates must belong to the calendar year")
        if len(self.holidays) != len(set(self.holidays)):
            raise ValueError("holiday dates must be unique")
        special_dates = [item.session_date for item in self.special_sessions]
        if len(special_dates) != len(set(special_dates)):
            raise ValueError("special session dates must be unique")
        change_dates = [item.effective_from for item in self.session_changes]
        if change_dates != sorted(set(change_dates)):
            raise ValueError("session changes must be unique and ordered")
        return self


class MarketCalendar:
    def __init__(self, config: CalendarConfig) -> None:
        self.config = config
        self.zone = ZoneInfo(config.timezone)

    @classmethod
    def load(cls, path: Path) -> MarketCalendar:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            return cls(CalendarConfig.model_validate(raw))
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise CalendarError("calendar_invalid", "Market calendar cannot be loaded") from error

    def session_times(self, day: date) -> SessionTimes | None:
        self._check_year(day)
        special = next(
            (item for item in self.config.special_sessions if item.session_date == day), None
        )
        if special is not None:
            return special.times if special.status == "confirmed" else None
        if day.weekday() >= 5 or day in self.config.holidays:
            return None
        change = next(
            (item for item in reversed(self.config.session_changes) if item.effective_from <= day),
            None,
        )
        return change.times if change is not None else self.config.regular_session

    def is_trading_day(self, day: date) -> bool:
        return self.session_times(day) is not None

    def is_market_open(self, moment: datetime) -> bool:
        local = self._local(moment)
        session = self.session_times(local.date())
        return session is not None and session.market_open <= local.time() <= session.market_close

    def is_strategy_window(self, moment: datetime) -> bool:
        local = self._local(moment)
        session = self.session_times(local.date())
        return (
            session is not None and session.strategy_start <= local.time() <= session.strategy_end
        )

    def next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        while candidate.year == self.config.year:
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise CalendarError("calendar_out_of_range", "No next trading day in calendar version")

    def _check_year(self, day: date) -> None:
        if day.year != self.config.year:
            raise CalendarError("calendar_out_of_range", "Date is outside calendar version")

    def _local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise CalendarError("calendar_time_naive", "Market time must be timezone-aware")
        return moment.astimezone(self.zone)
