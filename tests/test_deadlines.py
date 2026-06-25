from datetime import date
from zoneinfo import ZoneInfo

from meeting_bot.deadlines import deadline_datetime, iso_week_to_card_start, week_start_for
from meeting_bot.schema import DeadlineSpec


def test_week_start_and_deadline() -> None:
    assert week_start_for(date(2026, 6, 25), 1) == date(2026, 6, 22)
    due = deadline_datetime(
        date(2026, 6, 22),
        1,
        DeadlineSpec(day=7, hour=15, minute=30),
        ZoneInfo("Europe/Moscow"),
    )
    assert due.isoformat() == "2026-06-28T15:30:00+03:00"


def test_iso_week_normalizes_to_custom_start() -> None:
    assert iso_week_to_card_start(2026, 26, 7) == date(2026, 6, 21)
