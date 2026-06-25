import json
from datetime import datetime

import pytest
from sqlalchemy import select

from meeting_bot.access import AccessService
from meeting_bot.card_service import PermissionDenied, StaleChange
from meeting_bot.domain import PatchOperation
from meeting_bot.models import MeetingCard, PendingChange


async def approve_editor(database, app_config, user_id: int = 2) -> None:
    access = AccessService(database, app_config)
    await access.ensure_root_admin()
    await access.observe(
        user_id=user_id,
        username="editor",
        full_name="Editor",
        chat_id=user_id,
        chat_type="private",
        chat_title=None,
    )
    await access.decide_user(1, user_id, status="approved", role="editor")


async def test_empty_repeatable_hidden_and_added_entry_visible(
    card_service, database, app_config
) -> None:
    await approve_editor(database, app_config)
    card = await card_service.get_or_create_current(
        datetime(2026, 6, 25, tzinfo=app_config.timezone)
    )
    schema, _ = await card_service.schema_for_card(card)
    assert [block.block_id for block in card_service.status_blocks(card, schema)] == ["speaker"]

    pending = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[
            PatchOperation(
                op="add_entry",
                block_id="announcements",
                value="Лагерь",
                human_label="Лагерь",
            ),
            PatchOperation(
                op="set_field",
                block_id="announcements",
                field_id="approved",
                value="В процессе",
                human_label="Согласовано",
            ),
        ],
        now=datetime(2026, 6, 25, tzinfo=app_config.timezone),
    )
    before = await card_service.get_or_create_current(
        datetime(2026, 6, 25, tzinfo=app_config.timezone)
    )
    assert card_service.card_data(before)["blocks"]["announcements"] == []

    await card_service.resolve_pending(pending.id, 2, approve=True)
    after = await card_service.get_or_create_current(
        datetime(2026, 6, 25, tzinfo=app_config.timezone)
    )
    entry = card_service.card_data(after)["blocks"]["announcements"][0]
    assert entry["title"] == "Лагерь"
    assert entry["fields"]["title"] == "Лагерь"
    assert entry["fields"]["approved"] == "В процессе"
    assert len(card_service.status_blocks(after, schema)) == 2


async def test_viewer_cannot_create_patch(card_service, database, app_config) -> None:
    access = AccessService(database, app_config)
    await access.ensure_root_admin()
    await access.observe(
        user_id=5,
        username=None,
        full_name="Viewer",
        chat_id=5,
        chat_type="private",
        chat_title=None,
    )
    await access.decide_user(1, 5, status="approved", role="viewer")
    await card_service.get_or_create_current()
    with pytest.raises(PermissionDenied):
        await card_service.create_pending(
            user_id=5,
            chat_id=5,
            operations=[
                PatchOperation(
                    op="set_field",
                    block_id="speaker",
                    field_id="name",
                    value="Иван",
                )
            ],
        )


async def test_group_chat_cannot_create_patch(card_service, database, app_config) -> None:
    await approve_editor(database, app_config)
    access = AccessService(database, app_config)
    await access.observe(
        user_id=2,
        username="editor",
        full_name="Editor",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="Group",
    )
    await card_service.get_or_create_current()
    with pytest.raises(PermissionDenied, match="чате"):
        await card_service.create_pending(
            user_id=2,
            chat_id=-100,
            operations=[
                PatchOperation(
                    op="set_field",
                    block_id="speaker",
                    field_id="name",
                    value="Иван",
                )
            ],
        )


async def test_stale_pending_expires(card_service, database, app_config) -> None:
    await approve_editor(database, app_config)
    await card_service.get_or_create_current()
    first = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[
            PatchOperation(op="set_field", block_id="speaker", field_id="name", value="Первый")
        ],
    )
    second = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[
            PatchOperation(op="set_field", block_id="speaker", field_id="name", value="Второй")
        ],
    )
    await card_service.resolve_pending(first.id, 2, approve=True)
    with pytest.raises(StaleChange):
        await card_service.resolve_pending(second.id, 2, approve=True)
    async with database.session() as session:
        stored = await session.get(PendingChange, second.id)
        assert stored.status == "expired"


async def test_blocked_editor_cannot_confirm(card_service, database, app_config) -> None:
    await approve_editor(database, app_config)
    await card_service.get_or_create_current()
    pending = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[
            PatchOperation(op="set_field", block_id="speaker", field_id="name", value="Иван")
        ],
    )
    access = AccessService(database, app_config)
    await access.decide_user(1, 2, status="blocked")
    with pytest.raises(PermissionDenied):
        await card_service.resolve_pending(pending.id, 2, approve=True)


async def test_week_rollover_archives_and_rebinds_schema(
    card_service, database, app_config
) -> None:
    first = await card_service.get_or_create_current(
        datetime(2026, 6, 25, tzinfo=app_config.timezone)
    )
    second = await card_service.get_or_create_current(
        datetime(2026, 7, 2, tzinfo=app_config.timezone)
    )
    assert first.id != second.id
    async with database.session() as session:
        archived = await session.scalar(select(MeetingCard).where(MeetingCard.id == first.id))
        assert archived.archived_at is not None
        assert json.loads(archived.data_json)["blocks"]["announcements"] == []
