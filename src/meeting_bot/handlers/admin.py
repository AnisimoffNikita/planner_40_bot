from __future__ import annotations

import html

from telegram import Update
from telegram.ext import ContextTypes

from meeting_bot.command_catalog import sync_user_command_menu
from meeting_bot.handlers.common import require_access, send_long


def services(context: ContextTypes.DEFAULT_TYPE) -> object:
    return context.application.bot_data["services"]


async def _require_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> object | None:
    access = await require_access(update, context, command=command)
    if access is None:
        return None
    if access.user.role != "admin":
        await update.effective_message.reply_text("Команда доступна только root-admin.")
        return None
    return access


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_admin(update, context, "users") is None:
        return
    users = await services(context).access.users()
    lines = ["<b>Пользователи</b>"]
    lines.extend(
        f"• <code>{user.telegram_user_id}</code> "
        f"{html.escape(user.full_name or user.username or '—')} — "
        f"{html.escape(user.role)}/{html.escape(user.status)}"
        for user in users
    )
    await send_long(update.effective_message, "\n".join(lines))


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await _require_admin(update, context, "approve")
    if access is None:
        return
    if len(context.args) != 2 or context.args[1] not in {"viewer", "editor"}:
        await update.effective_message.reply_text("Формат: /approve ID viewer|editor")
        return
    user = await services(context).access.decide_user(
        access.user.telegram_user_id,
        int(context.args[0]),
        status="approved",
        role=context.args[1],
    )
    await sync_user_command_menu(context.bot, user)
    await update.effective_message.reply_text(f"Пользователь {user.telegram_user_id} одобрен.")
    await context.bot.send_message(user.telegram_user_id, f"Доступ одобрен. Роль: {user.role}.")


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _user_status_command(update, context, "reject", "rejected")


async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _user_status_command(update, context, "block_user", "blocked")


async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _user_status_command(update, context, "unblock_user", "approved")


async def _user_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, status: str
) -> None:
    access = await _require_admin(update, context, command)
    if access is None:
        return
    if len(context.args) != 1 or not context.args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(f"Формат: /{command} ID")
        return
    user = await services(context).access.decide_user(
        access.user.telegram_user_id, int(context.args[0]), status=status
    )
    await sync_user_command_menu(context.bot, user)
    await update.effective_message.reply_text(
        f"Пользователь {user.telegram_user_id}: {user.status}."
    )
    try:
        await context.bot.send_message(
            user.telegram_user_id,
            "Доступ одобрен." if status == "approved" else f"Статус доступа: {status}.",
        )
    except Exception:
        pass


async def block_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _chat_status_command(update, context, "block_chat", True)


async def unblock_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _chat_status_command(update, context, "unblock_chat", False)


async def _chat_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, blocked: bool
) -> None:
    access = await _require_admin(update, context, command)
    if access is None:
        return
    if len(context.args) != 1 or not context.args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(f"Формат: /{command} CHAT_ID")
        return
    chat = await services(context).access.set_chat_blocked(
        access.user.telegram_user_id, int(context.args[0]), blocked
    )
    await update.effective_message.reply_text(f"Чат {chat.chat_id}: {chat.status}.")


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_admin(update, context, "audit") is None:
        return
    limit = 20
    if context.args and context.args[0].isdigit():
        limit = min(int(context.args[0]), 100)
    rows = await services(context).access.audit(limit)
    lines = ["<b>Audit log</b>"]
    lines.extend(
        f"• {row.created_at:%d.%m %H:%M} <code>{html.escape(row.action)}</code> "
        f"actor={row.actor_user_id or 'system'} target={html.escape(row.target_id or '—')}"
        for row in rows
    )
    await send_long(update.effective_message, "\n".join(lines))
