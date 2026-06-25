from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from meeting_bot.schema import DeadlineSpec


def week_start_for(day: date, week_starts_on: int) -> date:
    offset = (day.isoweekday() - week_starts_on) % 7
    return day - timedelta(days=offset)


def deadline_datetime(
    week_start: date,
    week_starts_on: int,
    deadline: DeadlineSpec,
    timezone: ZoneInfo,
) -> datetime:
    day_offset = (deadline.day - week_starts_on) % 7
    deadline_date = week_start + timedelta(days=day_offset)
    return datetime.combine(deadline_date, time(deadline.hour, deadline.minute), tzinfo=timezone)


def iso_week_to_card_start(year: int, week: int, week_starts_on: int) -> date:
    iso_monday = date.fromisocalendar(year, week, 1)
    return week_start_for(iso_monday, week_starts_on)
