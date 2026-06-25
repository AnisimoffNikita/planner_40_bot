from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram import Bot

from meeting_bot.access import AccessService
from meeting_bot.card_service import CardService, StatusField
from meeting_bot.config import AppConfig
from meeting_bot.models import NotificationState
from meeting_bot.readiness import ValueState
from meeting_bot.storage import Database


class NotificationService:
    def __init__(
        self,
        database: Database,
        config: AppConfig,
        card_service: CardService,
        access_service: AccessService,
    ) -> None:
        self.database = database
        self.config = config
        self.card_service = card_service
        self.access_service = access_service

    async def run(self, bot: Bot, now: datetime | None = None) -> None:
        if not self.config.notifications.enabled:
            return
        now = now or datetime.now(self.config.timezone)
        card = await self.card_service.get_or_create_current(now)
        schema, _ = await self.card_service.schema_for_card(card)
        status = self.card_service.status_blocks(card, schema, now)
        window_end = now + timedelta(hours=self.config.notifications.remind_before_hours)
        due = [
            field
            for block in status
            for field in block.fields
            if field.evaluation.value_state not in {ValueState.READY, ValueState.OPTIONAL}
            and field.evaluation.deadline_at is not None
            and now < field.evaluation.deadline_at <= window_end
        ]
        if not due:
            return
        recipients = await self.access_service.approved_editors()
        for recipient in recipients:
            unsent = [
                field
                for field in due
                if not await self._already_sent(card.id, field, recipient.telegram_user_id)
            ]
            if not unsent:
                continue
            grouped: dict[str, list[StatusField]] = defaultdict(list)
            for field in unsent:
                grouped[field.block_title].append(field)
            lines = [
                f"Напоминание по собранию: дедлайн в ближайшие "
                f"{self.config.notifications.remind_before_hours} ч."
            ]
            for block_title, fields in grouped.items():
                lines.append(f"\n{block_title}:")
                for field in fields:
                    value = field.evaluation.value or "не заполнено"
                    entry = f" ({field.entry_title})" if field.entry_title else ""
                    lines.append(f"• {field.field_label}{entry}: {value}")
            await bot.send_message(recipient.telegram_user_id, "\n".join(lines))
            for field in unsent:
                await self._mark_sent(card.id, field, recipient.telegram_user_id)

    async def _already_sent(self, card_id: int, field: StatusField, user_id: int) -> bool:
        deadline_at = field.evaluation.deadline_at
        if deadline_at is None:
            return True
        stored_deadline = deadline_at.astimezone(UTC).replace(tzinfo=None)
        async with self.database.session() as session:
            existing = await session.scalar(
                select(NotificationState.id).where(
                    NotificationState.card_id == card_id,
                    NotificationState.block_id == field.block_id,
                    NotificationState.entry_key == (field.entry_id or ""),
                    NotificationState.field_id == field.field_id,
                    NotificationState.deadline_at == stored_deadline,
                    NotificationState.reminder_kind == "before_deadline",
                    NotificationState.sent_to_user_id == user_id,
                )
            )
            return existing is not None

    async def _mark_sent(self, card_id: int, field: StatusField, user_id: int) -> None:
        deadline_at = field.evaluation.deadline_at
        if deadline_at is None:
            return
        async with self.database.session() as session, session.begin():
            session.add(
                NotificationState(
                    card_id=card_id,
                    block_id=field.block_id,
                    entry_key=field.entry_id or "",
                    field_id=field.field_id,
                    deadline_at=deadline_at.astimezone(UTC).replace(tzinfo=None),
                    reminder_kind="before_deadline",
                    sent_to_user_id=user_id,
                    sent_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
