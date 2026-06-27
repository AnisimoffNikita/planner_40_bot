from __future__ import annotations

import html
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from meeting_bot.domain import Role, UserStatus

logger = logging.getLogger(__name__)


class MenuBot(Protocol):
    async def set_my_commands(
        self, commands: Sequence[BotCommand], *, scope: object | None = None
    ) -> object: ...


class MenuUser(Protocol):
    telegram_user_id: int
    role: str
    status: str


@dataclass(frozen=True)
class CommandSpec:
    name: str
    menu_description: str
    help_text: str
    section: str


COMMANDS: dict[str, CommandSpec] = {
    "start": CommandSpec(
        "start", "Регистрация и статус доступа", "/start — регистрация и статус", "base"
    ),
    "help": CommandSpec("help", "Помощь", "/help — помощь", "base"),
    "whoami": CommandSpec("whoami", "Роль и статус", "/whoami — роль и статус", "base"),
    "status": CommandSpec(
        "status", "PDF текущей карточки", "/status — PDF текущей карточки", "base"
    ),
    "summary": CommandSpec("summary", "Краткий статус", "/summary — краткий статус", "base"),
    "ask": CommandSpec("ask", "Вопрос по карточке", "/ask ТЕКСТ — вопрос по карточке", "base"),
    "history": CommandSpec("history", "Архив карточек", "/history [YYYY-WW] — архив", "base"),
    "schema": CommandSpec("schema", "Текущая схема", "/schema — версия и блоки", "base"),
    "update": CommandSpec(
        "update", "Обновить карточку кнопками", "/update — обновить карточку кнопками", "edit"
    ),
    "pending": CommandSpec(
        "pending", "Мои предложения", "/pending — ожидающие подтверждения", "edit"
    ),
    "cancel": CommandSpec(
        "cancel", "Отменить предложение", "/cancel ID — отменить предложение", "edit"
    ),
    "users": CommandSpec("users", "Пользователи", "/users — список пользователей", "admin"),
    "chats": CommandSpec("chats", "Чаты", "/chats — список чатов", "admin"),
    "approve": CommandSpec(
        "approve", "Одобрить пользователя", "/approve ID viewer|editor — одобрить", "admin"
    ),
    "approve_chat": CommandSpec(
        "approve_chat", "Одобрить чат", "/approve_chat CHAT_ID — одобрить чат", "admin"
    ),
    "reject_chat": CommandSpec(
        "reject_chat", "Отклонить чат", "/reject_chat CHAT_ID — отклонить чат", "admin"
    ),
    "reject": CommandSpec("reject", "Отклонить пользователя", "/reject ID — отклонить", "admin"),
    "block_user": CommandSpec(
        "block_user",
        "Заблокировать пользователя",
        "/block_user ID — заблокировать пользователя",
        "admin",
    ),
    "unblock_user": CommandSpec(
        "unblock_user",
        "Разблокировать пользователя",
        "/unblock_user ID — разблокировать пользователя",
        "admin",
    ),
    "block_chat": CommandSpec(
        "block_chat", "Заблокировать чат", "/block_chat ID — заблокировать чат", "admin"
    ),
    "unblock_chat": CommandSpec(
        "unblock_chat", "Разблокировать чат", "/unblock_chat ID — разблокировать чат", "admin"
    ),
    "audit": CommandSpec("audit", "Audit log", "/audit [limit] — audit", "admin"),
}

MINIMAL_PRIVATE_COMMANDS = ("start", "whoami")
VIEWER_PRIVATE_COMMANDS = (
    "start",
    "help",
    "whoami",
    "status",
    "summary",
    "ask",
    "history",
    "schema",
)
EDITOR_PRIVATE_COMMANDS = (*VIEWER_PRIVATE_COMMANDS, "update", "pending", "cancel")
ADMIN_PRIVATE_COMMANDS = (
    *EDITOR_PRIVATE_COMMANDS,
    "users",
    "chats",
    "approve",
    "approve_chat",
    "reject_chat",
    "reject",
    "block_user",
    "unblock_user",
    "block_chat",
    "unblock_chat",
    "audit",
)
GROUP_COMMANDS = ("help", "status", "summary", "ask")

SECTION_TITLES = {
    "base": "Команды",
    "edit": "Изменения",
    "admin": "Администрирование",
}
SECTION_ORDER = ("base", "edit", "admin")


def command_specs(names: Sequence[str]) -> list[CommandSpec]:
    return [COMMANDS[name] for name in names]


def private_command_specs(role: str, status: str) -> list[CommandSpec]:
    if status != UserStatus.APPROVED.value:
        return command_specs(MINIMAL_PRIVATE_COMMANDS)
    if role == Role.ADMIN.value:
        return command_specs(ADMIN_PRIVATE_COMMANDS)
    if role == Role.EDITOR.value:
        return command_specs(EDITOR_PRIVATE_COMMANDS)
    return command_specs(VIEWER_PRIVATE_COMMANDS)


def group_command_specs() -> list[CommandSpec]:
    return command_specs(GROUP_COMMANDS)


def command_specs_for_chat(chat_type: str, role: str, status: str) -> list[CommandSpec]:
    if chat_type in {"group", "supergroup"}:
        return group_command_specs()
    return private_command_specs(role, status)


def command_specs_for_user(user: MenuUser) -> list[CommandSpec]:
    return private_command_specs(user.role, user.status)


def bot_commands(specs: Sequence[CommandSpec]) -> list[BotCommand]:
    return [BotCommand(spec.name, spec.menu_description) for spec in specs]


def help_text(specs: Sequence[CommandSpec]) -> str:
    sections: list[str] = []
    for section in SECTION_ORDER:
        section_specs = [spec for spec in specs if spec.section == section]
        if not section_specs:
            continue
        lines = [f"<b>{SECTION_TITLES[section]}</b>"]
        lines.extend(html.escape(spec.help_text) for spec in section_specs)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


async def sync_default_command_menus(bot: MenuBot) -> None:
    await _set_commands_best_effort(
        bot,
        bot_commands(group_command_specs()),
        BotCommandScopeAllGroupChats(),
        "all group chats",
    )
    await _set_commands_best_effort(
        bot,
        bot_commands(command_specs(MINIMAL_PRIVATE_COMMANDS)),
        BotCommandScopeAllPrivateChats(),
        "all private chats",
    )


async def sync_user_command_menu(bot: MenuBot, user: MenuUser) -> None:
    await _set_commands_best_effort(
        bot,
        bot_commands(command_specs_for_user(user)),
        BotCommandScopeChat(chat_id=user.telegram_user_id),
        f"private chat {user.telegram_user_id}",
    )


async def sync_all_user_command_menus(bot: MenuBot, users: Sequence[MenuUser]) -> None:
    for user in users:
        await sync_user_command_menu(bot, user)


async def sync_all_command_menus(bot: MenuBot, users: Sequence[MenuUser]) -> None:
    await sync_default_command_menus(bot)
    await sync_all_user_command_menus(bot, users)


async def _set_commands_best_effort(
    bot: MenuBot,
    commands: Sequence[BotCommand],
    scope: object,
    label: str,
) -> None:
    try:
        await bot.set_my_commands(commands, scope=scope)
    except Exception:
        logger.warning("Failed to sync Telegram command menu for %s", label, exc_info=True)
