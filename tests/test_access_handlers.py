from __future__ import annotations

import logging
from types import SimpleNamespace

from telegram.error import BadRequest

from meeting_bot.access import AccessService
from meeting_bot.handlers import commands


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class AdminChatNotFoundBot:
    async def send_message(self, *args: object, **kwargs: object) -> None:
        raise BadRequest("Chat not found")


class QuietBot:
    async def send_message(self, *args: object, **kwargs: object) -> None:
        return None


def make_update(*, user_id: int, username: str | None = None, full_name: str = "User"):
    message = FakeMessage()
    user = SimpleNamespace(id=user_id, username=username, full_name=full_name)
    chat = SimpleNamespace(id=user_id, type="private", title=None)
    return SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message)


def make_context(access: AccessService, app_config, bot):
    services = SimpleNamespace(access=access, config=app_config)
    application = SimpleNamespace(bot_data={"services": services})
    return SimpleNamespace(application=application, bot=bot, args=[])


async def test_pending_user_saved_when_root_admin_chat_is_missing(
    database, app_config, caplog
) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    update = make_update(user_id=2, username="newbie", full_name="New User")
    context = make_context(service, app_config, AdminChatNotFoundBot())

    with caplog.at_level(logging.WARNING):
        await commands.start(update, context)

    users = await service.users()
    pending = next(user for user in users if user.telegram_user_id == 2)
    assert pending.status == "pending"
    assert update.effective_message.replies == [
        "Ты пока не одобрен. Администратор еще не открыл диалог с ботом или "
        "admin_user_id настроен неверно. Заявка сохранена."
    ]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("Telegram chat not found" in record.message for record in caplog.records)


async def test_root_admin_start_mentions_saved_pending_users(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=2,
        username="newbie",
        full_name="New User",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    update = make_update(user_id=1, username="admin", full_name="Root Admin")
    context = make_context(service, app_config, QuietBot())

    await commands.start(update, context)

    assert update.effective_message.replies == [
        "Доступ активен: <b>admin</b>. Используй /help, чтобы увидеть команды."
        "\n\nОжидают решения заявки: 1. Открой /users."
    ]
