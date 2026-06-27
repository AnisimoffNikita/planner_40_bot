from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from meeting_bot.domain import IntentResult, PatchOperation
from meeting_bot.models import ClarificationSession
from meeting_bot.schema import MeetingSchema
from meeting_bot.storage import Database

INTENT_JSON_SCHEMA = {
    "name": "meeting_intent",
    "strict": True,
    "schema": IntentResult.model_json_schema(),
}


def validate_intent_domain(result: IntentResult, schema: MeetingSchema) -> IntentResult:
    block_map = schema.block_map
    for patch in result.patches:
        block = block_map.get(patch.block_id)
        if block is None:
            raise ValueError(f"Unknown block: {patch.block_id}")
        if patch.op in {"set_field", "clear_field"} and patch.field_id not in block.fields:
            raise ValueError(f"Unknown field: {patch.block_id}.{patch.field_id}")
        if patch.op in {"add_entry", "delete_entry"} and not block.multiple:
            raise ValueError(f"Block is not repeatable: {patch.block_id}")
        if patch.op == "clear_block" and block.multiple:
            raise ValueError(f"Block is repeatable: {patch.block_id}")
    if result.needs_clarification and not result.clarification_question:
        raise ValueError("clarification_question is required")
    return result


class ClarificationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def save(
        self,
        user_id: int,
        chat_id: int,
        original_text: str,
        question: str,
        patches: list[PatchOperation],
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self.database.session() as session, session.begin():
            existing = await session.get(ClarificationSession, user_id)
            if existing is None:
                existing = ClarificationSession(
                    user_id=user_id,
                    chat_id=chat_id,
                    original_text=original_text,
                    context_json=json.dumps(
                        [patch.model_dump() for patch in patches], ensure_ascii=False
                    ),
                    question=question,
                    created_at=now,
                    expires_at=now + timedelta(minutes=30),
                )
                session.add(existing)
            else:
                existing.chat_id = chat_id
                existing.original_text = original_text
                existing.context_json = json.dumps(
                    [patch.model_dump() for patch in patches], ensure_ascii=False
                )
                existing.question = question
                existing.created_at = now
                existing.expires_at = now + timedelta(minutes=30)

    async def consume(self, user_id: int, chat_id: int) -> ClarificationSession | None:
        async with self.database.session() as session, session.begin():
            existing = await session.get(ClarificationSession, user_id)
            if (
                existing is None
                or existing.chat_id != chat_id
                or existing.expires_at < datetime.now(UTC).replace(tzinfo=None)
            ):
                if existing is not None:
                    await session.delete(existing)
                return None
            await session.delete(existing)
            return existing

    async def cleanup(self) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(
                delete(ClarificationSession).where(
                    ClarificationSession.expires_at < datetime.now(UTC).replace(tzinfo=None)
                )
            )
