from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meeting_bot.config import AppConfig
from meeting_bot.deadlines import iso_week_to_card_start, week_start_for
from meeting_bot.domain import PatchOperation, Role
from meeting_bot.models import (
    AuditLog,
    Chat,
    MeetingCard,
    PendingChange,
    SchemaSnapshot,
    User,
)
from meeting_bot.readiness import FieldEvaluation, ValueState, evaluate_field
from meeting_bot.schema import BlockSpec, LoadedSchema, MeetingSchema
from meeting_bot.storage import Database


class DomainError(ValueError):
    pass


class PermissionDenied(DomainError):
    pass


class StaleChange(DomainError):
    pass


@dataclass(frozen=True)
class StatusField:
    block_id: str
    block_title: str
    entry_id: str | None
    entry_title: str | None
    field_id: str
    field_label: str
    evaluation: FieldEvaluation


@dataclass(frozen=True)
class StatusBlock:
    block_id: str
    title: str
    entry_id: str | None
    entry_title: str | None
    fields: list[StatusField]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def empty_card_data(schema: MeetingSchema) -> dict[str, Any]:
    return {
        "blocks": {block.id: [] if block.multiple else {"fields": {}} for block in schema.blocks}
    }


class CardService:
    def __init__(self, database: Database, config: AppConfig, loaded_schema: LoadedSchema):
        self.database = database
        self.config = config
        self.loaded_schema = loaded_schema

    async def ensure_schema_snapshot(self, session: AsyncSession) -> None:
        snapshot = await session.get(SchemaSnapshot, self.loaded_schema.schema_hash)
        if snapshot is None:
            session.add(
                SchemaSnapshot(
                    schema_hash=self.loaded_schema.schema_hash,
                    schema_version=self.loaded_schema.schema.version,
                    schema_json=self.loaded_schema.canonical_json,
                    created_at=utcnow(),
                )
            )

    async def get_or_create_current(self, now: datetime | None = None) -> MeetingCard:
        now = now or datetime.now(self.config.timezone)
        week_start = week_start_for(
            now.astimezone(self.config.timezone).date(), self.config.meeting.week_starts_on
        )
        async with self.database.session() as session, session.begin():
            await self.ensure_schema_snapshot(session)
            card = await session.scalar(
                select(MeetingCard).where(MeetingCard.week_start_date == week_start.isoformat())
            )
            if card is None:
                await session.execute(
                    update(MeetingCard)
                    .where(MeetingCard.archived_at.is_(None))
                    .values(archived_at=utcnow(), updated_at=utcnow())
                )
                await session.execute(
                    update(PendingChange)
                    .where(
                        PendingChange.status == "pending",
                        PendingChange.week_start_date != week_start.isoformat(),
                    )
                    .values(status="expired", resolved_at=utcnow())
                )
                card = MeetingCard(
                    week_start_date=week_start.isoformat(),
                    schema_version=self.loaded_schema.schema.version,
                    schema_hash=self.loaded_schema.schema_hash,
                    data_json=json.dumps(
                        empty_card_data(self.loaded_schema.schema), ensure_ascii=False
                    ),
                    revision=0,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                    archived_at=None,
                )
                session.add(card)
                await session.flush()
                session.add(
                    AuditLog(
                        actor_user_id=None,
                        chat_id=None,
                        action="card_created",
                        target_type="meeting_card",
                        target_id=str(card.id),
                        details_json=json.dumps({"week_start_date": card.week_start_date}),
                        created_at=utcnow(),
                    )
                )
            elif card.schema_hash != self.loaded_schema.schema_hash and card.archived_at is None:
                data = self._merge_schema_defaults(self.card_data(card))
                card.schema_hash = self.loaded_schema.schema_hash
                card.schema_version = self.loaded_schema.schema.version
                card.data_json = json.dumps(data, ensure_ascii=False)
                card.revision += 1
                card.updated_at = utcnow()
            return card

    def _merge_schema_defaults(self, data: dict[str, Any]) -> dict[str, Any]:
        blocks = data.setdefault("blocks", {})
        for block in self.loaded_schema.schema.blocks:
            blocks.setdefault(block.id, [] if block.multiple else {"fields": {}})
        return data

    @staticmethod
    def card_data(card: MeetingCard) -> dict[str, Any]:
        raw = json.loads(card.data_json)
        if not isinstance(raw, dict):
            raise DomainError("Card JSON is corrupted")
        return raw

    async def history(self, limit: int = 10) -> list[MeetingCard]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(MeetingCard).order_by(desc(MeetingCard.week_start_date)).limit(limit)
            )
            return list(result)

    async def card_for_iso_week(self, year: int, week: int) -> MeetingCard | None:
        start = iso_week_to_card_start(year, week, self.config.meeting.week_starts_on)
        async with self.database.session() as session:
            return cast(
                MeetingCard | None,
                await session.scalar(
                    select(MeetingCard).where(MeetingCard.week_start_date == start.isoformat())
                ),
            )

    async def schema_for_card(self, card: MeetingCard) -> tuple[MeetingSchema, bool]:
        if card.schema_hash == self.loaded_schema.schema_hash:
            return self.loaded_schema.schema, False
        async with self.database.session() as session:
            snapshot = await session.get(SchemaSnapshot, card.schema_hash)
        if snapshot is None:
            return self.loaded_schema.schema, True
        return MeetingSchema.model_validate(json.loads(snapshot.schema_json)), False

    def status_blocks(
        self,
        card: MeetingCard,
        schema: MeetingSchema,
        now: datetime | None = None,
    ) -> list[StatusBlock]:
        now = now or datetime.now(self.config.timezone)
        week_start = date.fromisoformat(card.week_start_date)
        data_blocks = self.card_data(card).get("blocks", {})
        result: list[StatusBlock] = []
        for block in schema.blocks:
            stored = data_blocks.get(block.id)
            if block.multiple:
                if not isinstance(stored, list) or not stored:
                    continue
                for entry in stored:
                    if not isinstance(entry, dict):
                        continue
                    result.append(
                        self._status_block(
                            card,
                            block,
                            entry.get("fields", {}),
                            week_start,
                            now,
                            entry_id=str(entry.get("entry_id", "")),
                            entry_title=str(entry.get("title", "")) or None,
                        )
                    )
            else:
                fields = stored.get("fields", {}) if isinstance(stored, dict) else {}
                result.append(self._status_block(card, block, fields, week_start, now))
        return result

    def _status_block(
        self,
        card: MeetingCard,
        block: BlockSpec,
        values: dict[str, Any],
        week_start: date,
        now: datetime,
        entry_id: str | None = None,
        entry_title: str | None = None,
    ) -> StatusBlock:
        del card
        fields = [
            StatusField(
                block_id=block.id,
                block_title=block.title,
                entry_id=entry_id,
                entry_title=entry_title,
                field_id=field_id,
                field_label=field.label,
                evaluation=evaluate_field(
                    values.get(field_id),
                    field,
                    week_start=week_start,
                    week_starts_on=self.config.meeting.week_starts_on,
                    now=now,
                    timezone=self.config.timezone,
                ),
            )
            for field_id, field in block.fields.items()
        ]
        return StatusBlock(block.id, block.title, entry_id, entry_title, fields)

    def summary(self, status_blocks: list[StatusBlock]) -> dict[str, Any]:
        fields = [field for block in status_blocks for field in block.fields]
        relevant = [
            field for field in fields if field.evaluation.value_state != ValueState.OPTIONAL
        ]
        return {
            "total": len(relevant),
            "ready": sum(f.evaluation.value_state == ValueState.READY for f in relevant),
            "overdue": sum(f.evaluation.deadline_state == "overdue" for f in relevant),
            "due_today": sum(f.evaluation.deadline_state == "due_today" for f in relevant),
            "fields": fields,
        }

    async def create_pending(
        self,
        *,
        user_id: int,
        chat_id: int,
        operations: list[PatchOperation],
        now: datetime | None = None,
    ) -> PendingChange:
        now = now or datetime.now(self.config.timezone)
        async with self.database.session() as session, session.begin():
            user = await session.get(User, user_id)
            if (
                user is None
                or user.status != "approved"
                or user.role
                not in {
                    Role.EDITOR.value,
                    Role.ADMIN.value,
                }
            ):
                raise PermissionDenied("Твой доступ к изменению карточки недоступен.")
            chat = await session.get(Chat, chat_id)
            if (
                chat is None
                or chat.status != "approved"
                or chat.read_only
                or chat.chat_type != "private"
            ):
                raise PermissionDenied("В этом чате изменять карточку нельзя.")
            card = await self._current_in_session(session, now)
            normalized = self.validate_and_normalize_operations(card, operations)
            preview = self.preview(normalized)
            pending = PendingChange(
                created_by_user_id=user_id,
                chat_id=chat_id,
                week_start_date=card.week_start_date,
                precondition_revision=card.revision,
                patch_json=json.dumps(
                    [operation.model_dump() for operation in normalized], ensure_ascii=False
                ),
                preview_text=preview,
                status="pending",
                telegram_message_id=None,
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(hours=24),
                resolved_at=None,
            )
            session.add(pending)
            await session.flush()
            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    chat_id=chat_id,
                    action="patch_proposed",
                    target_type="pending_change",
                    target_id=str(pending.id),
                    details_json=json.dumps({"operations": len(normalized)}),
                    created_at=utcnow(),
                )
            )
            return pending

    async def _current_in_session(self, session: AsyncSession, now: datetime) -> MeetingCard:
        week_start = week_start_for(
            now.astimezone(self.config.timezone).date(), self.config.meeting.week_starts_on
        )
        card = await session.scalar(
            select(MeetingCard).where(MeetingCard.week_start_date == week_start.isoformat())
        )
        if card is None:
            raise DomainError("Current card has not been initialized")
        return card

    def validate_and_normalize_operations(
        self, card: MeetingCard, operations: list[PatchOperation]
    ) -> list[PatchOperation]:
        if not operations:
            raise DomainError("Изменение не содержит операций.")
        data = copy.deepcopy(self.card_data(card))
        normalized: list[PatchOperation] = []
        pending_adds: dict[str, list[str]] = {}
        for raw_operation in operations:
            operation = raw_operation.model_copy(deep=True)
            block = self.loaded_schema.schema.block_map.get(operation.block_id)
            if block is None:
                raise DomainError(f"Я не нашел блок {operation.block_id} в текущей схеме.")
            if operation.op == "add_entry":
                if not block.multiple:
                    raise DomainError(f"Блок {block.id} не является повторяемым.")
                operation.entry_id = operation.entry_id or str(uuid4())
                title = (operation.value or operation.human_label).strip()
                if not title:
                    raise DomainError("Для нового экземпляра нужно название.")
                operation.value = title
                pending_adds.setdefault(block.id, []).append(operation.entry_id)
            elif operation.op == "set_field":
                if operation.field_id not in block.fields:
                    raise DomainError(
                        f"Я не нашел поле {block.id}.{operation.field_id} в текущей схеме."
                    )
                if operation.value is None or not operation.value.strip():
                    raise DomainError("Пустое значение записать нельзя.")
                operation.value = operation.value.strip()
                if block.multiple and operation.entry_id is None:
                    candidates = pending_adds.get(block.id, [])
                    if len(candidates) != 1:
                        raise DomainError(
                            f"Этот блок повторяемый, нужно выбрать конкретный экземпляр {block.id}."
                        )
                    operation.entry_id = candidates[0]
                if not block.multiple and operation.entry_id is not None:
                    raise DomainError(f"Блок {block.id} не принимает entry_id.")
            elif operation.op == "delete_entry":
                if not block.multiple:
                    raise DomainError(f"Блок {block.id} не является повторяемым.")
            self._apply_operation(data, operation, block)
            normalized.append(operation)
        return normalized

    def _apply_operation(
        self, data: dict[str, Any], operation: PatchOperation, block: BlockSpec
    ) -> None:
        blocks = data.setdefault("blocks", {})
        if block.multiple:
            entries = blocks.setdefault(block.id, [])
            if not isinstance(entries, list):
                raise DomainError(f"Данные блока {block.id} повреждены.")
            if operation.op == "add_entry":
                if any(entry.get("entry_id") == operation.entry_id for entry in entries):
                    raise DomainError("Экземпляр с таким entry_id уже существует.")
                fields: dict[str, str] = {}
                if "title" in block.fields and operation.value:
                    fields["title"] = operation.value
                entries.append(
                    {"entry_id": operation.entry_id, "title": operation.value, "fields": fields}
                )
                return
            entry = next(
                (item for item in entries if item.get("entry_id") == operation.entry_id), None
            )
            if entry is None:
                raise DomainError(f"Я не нашел экземпляр {block.id}[{operation.entry_id}].")
            if operation.op == "delete_entry":
                entries.remove(entry)
            else:
                entry.setdefault("fields", {})[operation.field_id] = operation.value
        else:
            stored = blocks.setdefault(block.id, {"fields": {}})
            if not isinstance(stored, dict):
                raise DomainError(f"Данные блока {block.id} повреждены.")
            stored.setdefault("fields", {})[operation.field_id] = operation.value

    def preview(self, operations: list[PatchOperation]) -> str:
        lines = ["Предлагаемые изменения:"]
        for operation in operations:
            block = self.loaded_schema.schema.block_map[operation.block_id]
            if operation.op == "add_entry":
                lines.append(f"• Добавить «{operation.value}» в «{block.title}»")
            elif operation.op == "delete_entry":
                lines.append(f"• Удалить экземпляр {operation.entry_id} из «{block.title}»")
            else:
                field = block.fields[operation.field_id or ""]
                suffix = f" [{operation.entry_id}]" if operation.entry_id else ""
                lines.append(f"• {block.title}{suffix} — {field.label}: {operation.value}")
        lines.append("\nДанные изменятся только после подтверждения.")
        return "\n".join(lines)

    async def resolve_pending(
        self, pending_id: int, user_id: int, *, approve: bool
    ) -> PendingChange:
        stale_message: str | None = None
        async with self.database.session() as session, session.begin():
            pending = await session.get(PendingChange, pending_id)
            if pending is None:
                raise DomainError("Изменение не найдено.")
            if pending.created_by_user_id != user_id:
                raise PermissionDenied("Подтвердить может только автор изменения.")
            if pending.status != "pending":
                return pending
            user = await session.get(User, user_id)
            chat = await session.get(Chat, pending.chat_id)
            if (
                user is None
                or user.status != "approved"
                or user.role not in {Role.EDITOR.value, Role.ADMIN.value}
                or chat is None
                or chat.status != "approved"
                or chat.read_only
                or chat.chat_type != "private"
            ):
                raise PermissionDenied("Доступ к подтверждению изменения недоступен.")
            now = utcnow()
            if pending.expires_at < now:
                pending.status = "expired"
                pending.resolved_at = now
                return pending
            if not approve:
                pending.status = "cancelled"
                pending.resolved_at = now
                action = "patch_cancelled"
            else:
                card = await session.scalar(
                    select(MeetingCard).where(
                        MeetingCard.week_start_date == pending.week_start_date
                    )
                )
                if card is None or card.archived_at is not None:
                    pending.status = "expired"
                    pending.resolved_at = now
                    stale_message = "Неделя уже завершена; изменение устарело."
                    action = "patch_expired"
                elif card.revision != pending.precondition_revision:
                    pending.status = "expired"
                    pending.resolved_at = now
                    stale_message = "Карточка уже изменилась. Создай предложение заново."
                    action = "patch_expired"
                else:
                    data = copy.deepcopy(self.card_data(card))
                    operations = [
                        PatchOperation.model_validate(item)
                        for item in json.loads(pending.patch_json)
                    ]
                    for operation in operations:
                        block = self.loaded_schema.schema.block_map[operation.block_id]
                        self._apply_operation(data, operation, block)
                    card.data_json = json.dumps(data, ensure_ascii=False)
                    card.revision += 1
                    card.updated_at = now
                    pending.status = "approved"
                    pending.resolved_at = now
                    action = "patch_applied"
            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    chat_id=pending.chat_id,
                    action=action,
                    target_type="pending_change",
                    target_id=str(pending.id),
                    details_json="{}",
                    created_at=now,
                )
            )
        if stale_message is not None:
            raise StaleChange(stale_message)
        return pending

    async def pending_for_user(self, user_id: int) -> list[PendingChange]:
        async with self.database.session() as session:
            result = await session.scalars(
                select(PendingChange)
                .where(
                    PendingChange.created_by_user_id == user_id,
                    PendingChange.status == "pending",
                )
                .order_by(desc(PendingChange.created_at))
            )
            return list(result)

    async def set_pending_message_id(self, pending_id: int, message_id: int) -> None:
        async with self.database.session() as session, session.begin():
            pending = await session.get(PendingChange, pending_id)
            if pending is not None:
                pending.telegram_message_id = message_id
