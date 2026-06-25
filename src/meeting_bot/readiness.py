from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from meeting_bot.deadlines import deadline_datetime
from meeting_bot.schema import FieldSpec


class ValueState(StrEnum):
    READY = "ready"
    OPTIONAL = "optional"
    MISSING = "missing"
    IN_PROGRESS = "in_progress"


class DeadlineState(StrEnum):
    NONE = "none"
    NOT_DUE = "not_due"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"


@dataclass(frozen=True)
class FieldEvaluation:
    value: str | None
    value_state: ValueState
    deadline_state: DeadlineState
    deadline_at: datetime | None

    @property
    def is_problem(self) -> bool:
        return self.value_state not in {ValueState.READY, ValueState.OPTIONAL}


def normalize_value(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def canonicalize_allowed(value: str | None, field: FieldSpec) -> str | None:
    if value is None:
        return None
    folded = value.casefold()
    for allowed in field.allowed_values:
        if allowed.casefold() == folded:
            return allowed
    return value


def evaluate_value(value: object | None, field: FieldSpec) -> ValueState:
    normalized = canonicalize_allowed(normalize_value(value), field)
    rules = field.ready_if
    if "Любое значение или его отсутствие" in rules:
        return ValueState.OPTIONAL
    if normalized is None:
        return ValueState.MISSING
    if "Не пусто" in rules or "Любое значение" in rules:
        return ValueState.READY

    folded = normalized.casefold()
    exact_rules = {
        rule.casefold() for rule in rules if not (rule.startswith("<") and rule.endswith(">"))
    }
    if folded in exact_rules:
        return ValueState.READY

    has_placeholder = any(rule.startswith("<") and rule.endswith(">") for rule in rules)
    intermediate = {"в процессе", "ожидается", "не готово"}
    if has_placeholder and folded not in intermediate:
        return ValueState.READY
    return ValueState.IN_PROGRESS


def evaluate_field(
    value: object | None,
    field: FieldSpec,
    *,
    week_start: date,
    week_starts_on: int,
    now: datetime,
    timezone: ZoneInfo,
) -> FieldEvaluation:
    value_state = evaluate_value(value, field)
    normalized = canonicalize_allowed(normalize_value(value), field)
    if field.deadline is None or value_state in {ValueState.READY, ValueState.OPTIONAL}:
        return FieldEvaluation(normalized, value_state, DeadlineState.NONE, None)

    due_at = deadline_datetime(week_start, week_starts_on, field.deadline, timezone)
    local_now = now.astimezone(timezone)
    if local_now > due_at:
        deadline_state = DeadlineState.OVERDUE
    elif local_now.date() == due_at.date():
        deadline_state = DeadlineState.DUE_TODAY
    else:
        deadline_state = DeadlineState.NOT_DUE
    return FieldEvaluation(normalized, value_state, deadline_state, due_at)
