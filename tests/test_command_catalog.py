from __future__ import annotations

import logging
from types import SimpleNamespace

from telegram import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from telegram.error import BadRequest

from meeting_bot.command_catalog import (
    group_command_specs,
    private_command_specs,
    sync_all_command_menus,
    sync_user_command_menu,
)


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[tuple[list[object], object]] = []

    async def set_my_commands(self, commands: list[object], *, scope: object | None = None) -> None:
        self.calls.append((commands, scope))


class FailingBot:
    async def set_my_commands(self, commands: list[object], *, scope: object | None = None) -> None:
        raise BadRequest("menu unavailable")


def command_names(commands: list[object]) -> list[str]:
    return [command.command for command in commands]


def test_private_command_specs_follow_role_and_status() -> None:
    assert [spec.name for spec in private_command_specs("viewer", "approved")] == [
        "start",
        "help",
        "whoami",
        "status",
        "summary",
        "ask",
        "history",
        "schema",
    ]
    assert "update" in [spec.name for spec in private_command_specs("editor", "approved")]
    assert "users" in [spec.name for spec in private_command_specs("admin", "approved")]
    assert "chats" in [spec.name for spec in private_command_specs("admin", "approved")]
    assert "approve_chat" in [spec.name for spec in private_command_specs("admin", "approved")]
    assert [spec.name for spec in private_command_specs("admin", "blocked")] == [
        "start",
        "whoami",
    ]
    assert [spec.name for spec in group_command_specs()] == ["help", "status", "summary", "ask"]


async def test_sync_all_command_menus_sets_default_and_per_user_scopes() -> None:
    bot = RecordingBot()
    users = [
        SimpleNamespace(telegram_user_id=1, role="admin", status="approved"),
        SimpleNamespace(telegram_user_id=2, role="viewer", status="approved"),
        SimpleNamespace(telegram_user_id=3, role="editor", status="blocked"),
    ]

    await sync_all_command_menus(bot, users)

    assert len(bot.calls) == 5
    group_commands, group_scope = bot.calls[0]
    assert isinstance(group_scope, BotCommandScopeAllGroupChats)
    assert command_names(group_commands) == ["help", "status", "summary", "ask"]

    private_commands, private_scope = bot.calls[1]
    assert isinstance(private_scope, BotCommandScopeAllPrivateChats)
    assert command_names(private_commands) == ["start", "whoami"]

    admin_commands, admin_scope = bot.calls[2]
    assert isinstance(admin_scope, BotCommandScopeChat)
    assert admin_scope.chat_id == 1
    assert "users" in command_names(admin_commands)

    blocked_commands, blocked_scope = bot.calls[4]
    assert isinstance(blocked_scope, BotCommandScopeChat)
    assert blocked_scope.chat_id == 3
    assert command_names(blocked_commands) == ["start", "whoami"]


async def test_sync_user_command_menu_failure_is_logged(caplog) -> None:
    user = SimpleNamespace(telegram_user_id=2, role="editor", status="approved")

    with caplog.at_level(logging.WARNING):
        await sync_user_command_menu(FailingBot(), user)

    assert any(
        "Failed to sync Telegram command menu" in record.message for record in caplog.records
    )
