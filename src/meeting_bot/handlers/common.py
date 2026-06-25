from __future__ import annotations

import html
import logging
from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from meeting_bot.access import AccessContext

logger = logging.getLogger(__name__)
MAX_MESSAGE = 3900
SAFE_GROUP_COMMANDS = {"help", "status", "summary"}


async def observe_access(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> AccessContext | None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or chat.type == "channel":
        return None
    services = context.application.bot_data["services"]
    access = await services.access.observe(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat.title,
    )
    if access.is_new_user and access.user.status == "pending":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Viewer", callback_data=f"u:v:{user.id}"),
                    InlineKeyboardButton("Editor", callback_data=f"u:e:{user.id}"),
                ],
                [InlineKeyboardButton("Reject", callback_data=f"u:r:{user.id}")],
            ]
        )
        try:
            await context.bot.send_message(
                services.config.telegram.admin_user_id,
                f"Новая заявка: {html.escape(user.full_name)}\n"
                f"ID: <code>{user.id}</code>\n"
                f"Username: @{html.escape(user.username or '—')}",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to notify root admin about a new user")
    return access


async def require_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    approved: bool = True,
    editable: bool = False,
    command: str | None = None,
) -> AccessContext | None:
    access = await observe_access(update, context)
    message = update.effective_message
    if access is None or message is None:
        return None
    if access.blocked:
        await context.application.bot_data["services"].access.log_blocked_attempt(
            access.user.telegram_user_id, access.chat.chat_id, "telegram_update"
        )
        await message.reply_text("Доступ заблокирован. Обратитесь к администратору.")
        return None
    if access.chat.chat_type in {"group", "supergroup"} and command not in SAFE_GROUP_COMMANDS:
        await message.reply_text("В группах карточку менять нельзя. Напиши мне в личку.")
        return None
    if approved and not access.approved:
        if access.user.status == "pending":
            await message.reply_text("Ты пока не одобрен. Я отправил заявку администратору.")
        else:
            await message.reply_text("Доступ не одобрен. Обратитесь к администратору.")
        return None
    if editable and not access.can_edit:
        await message.reply_text("Изменять карточку могут editor/admin только в личном чате.")
        return None
    return access


async def send_long(message: object, text: str, **kwargs: object) -> None:
    chunks = _chunks(text, MAX_MESSAGE)
    for index, chunk in enumerate(chunks):
        current_kwargs = kwargs if index == len(chunks) - 1 else {}
        await message.reply_text(chunk, **current_kwargs)


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = remaining.rfind("\n", 0, size)
        if split <= 0:
            split = size
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    return chunks


def pending_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Применить", callback_data=f"p:a:{pending_id}"),
                InlineKeyboardButton("❌ Отменить", callback_data=f"p:c:{pending_id}"),
            ]
        ]
    )


def format_status_fields(fields: Sequence[object], title: str) -> str:
    lines = [title]
    for field in fields:
        value = field.evaluation.value or "не заполнено"
        lines.append(f"• {field.block_title} — {field.field_label}: {value}")
    return "\n".join(lines)
