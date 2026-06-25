from datetime import datetime

from meeting_bot.access import AccessService
from meeting_bot.domain import PatchOperation
from meeting_bot.notifications import NotificationService


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


async def test_notification_window_and_dedup(card_service, database, app_config) -> None:
    access = AccessService(database, app_config)
    await access.ensure_root_admin()
    await access.observe(
        user_id=2,
        username="editor",
        full_name="Editor",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    await access.decide_user(1, 2, status="approved", role="editor")

    now = datetime(2026, 6, 28, 10, tzinfo=app_config.timezone)
    await card_service.get_or_create_current(now)
    pending = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[
            PatchOperation(
                op="set_field",
                block_id="speaker",
                field_id="slides",
                value="В процессе",
            )
        ],
        now=now,
    )
    await card_service.resolve_pending(pending.id, 2, approve=True)

    notifications = NotificationService(database, app_config, card_service, access)
    bot = FakeBot()
    await notifications.run(bot, now)
    first_count = len(bot.messages)
    await notifications.run(bot, now)

    assert first_count == 2  # root-admin and editor
    assert len(bot.messages) == first_count
    assert "Слайды" in bot.messages[0][1]
