from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from meeting_bot.access import AccessService
from meeting_bot.handlers import admin, callbacks, commands


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.edits: list[str] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs: object) -> None:
        self.edits.append(text)


class AdminChatNotFoundBot:
    async def send_message(self, *args: object, **kwargs: object) -> None:
        raise BadRequest("Chat not found")


class QuietBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.command_calls: list[tuple[object, object]] = []

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append((args[0], args[1]))
        return None

    async def set_my_commands(self, commands: object, *, scope: object | None = None) -> None:
        self.command_calls.append((commands, scope))
        return None


class MenuFailingBot(QuietBot):
    async def set_my_commands(self, commands: object, *, scope: object | None = None) -> None:
        raise BadRequest("menu unavailable")


def make_update(
    *,
    user_id: int,
    username: str | None = None,
    full_name: str = "User",
    chat_id: int | None = None,
    chat_type: str = "private",
):
    message = FakeMessage()
    user = SimpleNamespace(id=user_id, username=username, full_name=full_name)
    chat = SimpleNamespace(
        id=chat_id if chat_id is not None else user_id,
        type=chat_type,
        title=None,
    )
    return SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message)


def make_callback_update(*, user_id: int, data: str) -> object:
    update = make_update(user_id=user_id, username="admin", full_name="Root Admin")
    update.callback_query = FakeQuery(data)
    return update


def make_context(access: AccessService, app_config, bot, args: list[str] | None = None):
    services = SimpleNamespace(access=access, config=app_config)
    application = SimpleNamespace(bot_data={"services": services})
    return SimpleNamespace(application=application, bot=bot, args=args or [])


async def approve_user(service: AccessService, role: str, user_id: int = 2) -> None:
    await service.observe(
        user_id=user_id,
        username="user",
        full_name="User",
        chat_id=user_id,
        chat_type="private",
        chat_title=None,
    )
    await service.decide_user(1, user_id, status="approved", role=role)


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


async def test_help_for_private_viewer_shows_viewer_commands(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await approve_user(service, "viewer")
    update = make_update(user_id=2)
    context = make_context(service, app_config, QuietBot())

    await commands.help_command(update, context)

    reply = update.effective_message.replies[0]
    assert "/status" in reply
    assert "/history [YYYY-WW]" in reply
    assert "/update" not in reply
    assert "/users" not in reply


async def test_help_for_private_editor_shows_edit_commands(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await approve_user(service, "editor")
    update = make_update(user_id=2)
    context = make_context(service, app_config, QuietBot())

    await commands.help_command(update, context)

    reply = update.effective_message.replies[0]
    assert "/update" in reply
    assert "/pending" in reply
    assert "/cancel ID" in reply
    assert "/users" not in reply


async def test_help_for_private_admin_shows_admin_commands(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    update = make_update(user_id=1, username="admin", full_name="Root Admin")
    context = make_context(service, app_config, QuietBot())

    await commands.help_command(update, context)

    reply = update.effective_message.replies[0]
    assert "/update" in reply
    assert "/users" in reply
    assert "/approve ID viewer|editor" in reply
    assert "/audit [limit]" in reply


@pytest.mark.parametrize("role", ["editor", "admin"])
async def test_help_in_group_shows_only_group_safe_commands(
    database, app_config, role: str
) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    user_id = 1 if role == "admin" else 2
    if role == "editor":
        await approve_user(service, "editor", user_id=user_id)
    update = make_update(user_id=user_id, chat_id=-100, chat_type="supergroup")
    context = make_context(service, app_config, QuietBot())

    await commands.help_command(update, context)

    reply = update.effective_message.replies[0]
    assert "/help" in reply
    assert "/status" in reply
    assert "/summary" in reply
    assert "/whoami" not in reply
    assert "/update" not in reply
    assert "/users" not in reply


async def test_approve_command_sync_failure_does_not_break_approval(
    database, app_config, caplog
) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=2,
        username="user",
        full_name="User",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    bot = MenuFailingBot()
    update = make_update(user_id=1, username="admin", full_name="Root Admin")
    context = make_context(service, app_config, bot, args=["2", "viewer"])

    with caplog.at_level(logging.WARNING):
        await admin.approve_command(update, context)

    users = await service.users()
    target = next(user for user in users if user.telegram_user_id == 2)
    assert target.status == "approved"
    assert target.role == "viewer"
    assert update.effective_message.replies == ["Пользователь 2 одобрен."]
    assert bot.messages == [(2, "Доступ одобрен. Роль: viewer.")]
    assert any(
        "Failed to sync Telegram command menu" in record.message for record in caplog.records
    )


async def test_user_approval_callback_syncs_target_command_menu(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=2,
        username="user",
        full_name="User",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    bot = QuietBot()
    update = make_callback_update(user_id=1, data="u:e:2")
    context = make_context(service, app_config, bot)

    await callbacks.user_approval_callback(update, context)

    users = await service.users()
    target = next(user for user in users if user.telegram_user_id == 2)
    commands, scope = bot.command_calls[0]
    assert target.status == "approved"
    assert target.role == "editor"
    assert scope.chat_id == 2
    assert "update" in [command.command for command in commands]
    assert update.callback_query.edits == [
        "Пользователь <code>2</code>\nРешение: approved"
    ]
