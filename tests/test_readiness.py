from datetime import datetime
from zoneinfo import ZoneInfo

from meeting_bot.readiness import DeadlineState, ValueState, evaluate_field, evaluate_value
from meeting_bot.schema import DeadlineSpec, FieldSpec


def field(ready_if: list[str], deadline: DeadlineSpec | None = None) -> FieldSpec:
    return FieldSpec(
        label="Поле",
        allowed_values=["Да", "В процессе", "<Название>", "Не требуется"],
        ready_if=ready_if,
        deadline=deadline,
    )


def test_non_empty_and_placeholder() -> None:
    assert evaluate_value("  Иван  ", field(["Не пусто"])) == ValueState.READY
    assert evaluate_value("Проектор", field(["<Название>"])) == ValueState.READY
    assert evaluate_value("В процессе", field(["<Название>"])) == ValueState.IN_PROGRESS


def test_optional_is_neutral() -> None:
    assert evaluate_value(None, field(["Любое значение или его отсутствие"])) == ValueState.OPTIONAL


def test_overdue_and_due_today() -> None:
    tz = ZoneInfo("Europe/Moscow")
    spec = field(["Да"], DeadlineSpec(day=3, hour=10, minute=0))
    overdue = evaluate_field(
        "В процессе",
        spec,
        week_start=datetime(2026, 6, 22).date(),
        week_starts_on=1,
        now=datetime(2026, 6, 24, 11, tzinfo=tz),
        timezone=tz,
    )
    due_today = evaluate_field(
        None,
        spec,
        week_start=datetime(2026, 6, 22).date(),
        week_starts_on=1,
        now=datetime(2026, 6, 24, 9, tzinfo=tz),
        timezone=tz,
    )
    assert overdue.deadline_state == DeadlineState.OVERDUE
    assert overdue.value_state == ValueState.IN_PROGRESS
    assert due_today.deadline_state == DeadlineState.DUE_TODAY


def test_ready_has_no_deadline_problem() -> None:
    tz = ZoneInfo("Europe/Moscow")
    result = evaluate_field(
        "да",
        field(["Да"], DeadlineSpec(day=1, hour=0, minute=0)),
        week_start=datetime(2026, 6, 22).date(),
        week_starts_on=1,
        now=datetime(2026, 6, 25, tzinfo=tz),
        timezone=tz,
    )
    assert result.value_state == ValueState.READY
    assert result.deadline_state == DeadlineState.NONE
    assert result.value == "Да"
