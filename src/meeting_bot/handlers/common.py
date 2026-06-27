from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from meeting_bot.access import AccessContext

logger = logging.getLogger(__name__)
MAX_MESSAGE = 3900
SAFE_GROUP_COMMANDS = {"help", "status", "summary"}
PENDING_ACCESS_SENT = "Ты пока не одобрен. Я отправил заявку администратору."
PENDING_ACCESS_SAVED = "Ты пока не одобрен. Заявка сохранена и ожидает решения администратора."
PENDING_ACCESS_ADMIN_UNREACHABLE = (
    "Ты пока не одобрен. Администратор еще не открыл диалог с ботом или "
    "admin_user_id настроен неверно. Заявка сохранена."
)
PENDING_CHAT_SENT = "Этот чат пока не одобрен. Я отправил заявку администратору."
PENDING_CHAT_SAVED = "Этот чат пока не одобрен. Заявка сохранена и ожидает решения администратора."
PENDING_CHAT_ADMIN_UNREACHABLE = (
    "Этот чат пока не одобрен. Администратор еще не открыл диалог с ботом или "
    "admin_user_id настроен неверно. Заявка сохранена."
)


@asynccontextmanager
async def typing_indicator(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    interval_seconds: float = 4.0,
) -> AsyncIterator[None]:
    chat = update.effective_chat
    if chat is None:
        yield
        return

    async def send_typing() -> None:
        try:
            await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("Failed to send Telegram typing chat action", exc_info=True)

    async def refresh_typing() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await send_typing()

    await send_typing()
    task = asyncio.create_task(refresh_typing())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
        delivered = await _notify_root_admin_about_new_user(update, context, user.id)
        access = replace(access, admin_notification_delivered=delivered)
    if (
        access.is_new_chat
        and access.chat.chat_type in {"group", "supergroup"}
        and access.chat.status == "pending"
    ):
        delivered = await notify_root_admin_about_new_chat(
            context,
            chat_id=access.chat.chat_id,
            chat_type=access.chat.chat_type,
            chat_title=access.chat.title,
        )
        access = replace(access, chat_notification_delivered=delivered)
    return access


async def require_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    approved: bool = True,
    editable: bool = False,
    command: str | None = None,
    allow_group_read_only: bool = False,
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
    if (
        access.chat.chat_type in {"group", "supergroup"}
        and command not in SAFE_GROUP_COMMANDS
        and not allow_group_read_only
    ):
        await message.reply_text("В группах карточку менять нельзя. Напиши мне в личку.")
        return None
    if approved and not access.approved:
        if access.chat.chat_type in {"group", "supergroup"}:
            if access.chat.status == "pending":
                await message.reply_text(pending_chat_message(access))
            else:
                await message.reply_text("Этот чат не одобрен. Обратитесь к администратору.")
        elif access.user.status == "pending":
            await message.reply_text(pending_access_message(access))
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


def pending_access_message(access: AccessContext) -> str:
    if access.admin_notification_delivered is True:
        return PENDING_ACCESS_SENT
    if access.admin_notification_delivered is False:
        return PENDING_ACCESS_ADMIN_UNREACHABLE
    return PENDING_ACCESS_SAVED


def pending_chat_message(access: AccessContext) -> str:
    if access.chat_notification_delivered is True:
        return PENDING_CHAT_SENT
    if access.chat_notification_delivered is False:
        return PENDING_CHAT_ADMIN_UNREACHABLE
    return PENDING_CHAT_SAVED


async def _notify_root_admin_about_new_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    user = update.effective_user
    if user is None:
        return False
    services = context.application.bot_data["services"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Viewer", callback_data=f"u:v:{user_id}"),
                InlineKeyboardButton("Editor", callback_data=f"u:e:{user_id}"),
            ],
            [InlineKeyboardButton("Reject", callback_data=f"u:r:{user_id}")],
        ]
    )
    try:
        await context.bot.send_message(
            services.config.telegram.admin_user_id,
            f"Новая заявка: {html.escape(user.full_name)}\n"
            f"ID: <code>{user_id}</code>\n"
            f"Username: @{html.escape(user.username or '—')}",
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "chat not found" in str(exc).lower():
            logger.warning(
                "Could not notify root admin %s about new user %s: Telegram chat not found. "
                "Root admin must send /start to the bot first, or telegram.admin_user_id is wrong.",
                services.config.telegram.admin_user_id,
                user_id,
            )
            return False
        logger.exception("Failed to notify root admin about a new user")
        return False
    except Exception:
        logger.exception("Failed to notify root admin about a new user")
        return False
    return True


async def notify_root_admin_about_new_chat(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    chat_type: str,
    chat_title: str | None,
) -> bool:
    services = context.application.bot_data["services"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve chat", callback_data=f"c:a:{chat_id}"),
                InlineKeyboardButton("Reject", callback_data=f"c:r:{chat_id}"),
            ]
        ]
    )
    try:
        await context.bot.send_message(
            services.config.telegram.admin_user_id,
            f"Новая заявка чата: {html.escape(chat_title or '—')}\n"
            f"ID: <code>{chat_id}</code>\n"
            f"Тип: <code>{html.escape(chat_type)}</code>",
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "chat not found" in str(exc).lower():
            logger.warning(
                "Could not notify root admin %s about new chat %s: Telegram chat not found. "
                "Root admin must send /start to the bot first, or telegram.admin_user_id is wrong.",
                services.config.telegram.admin_user_id,
                chat_id,
            )
            return False
        logger.exception("Failed to notify root admin about a new chat")
        return False
    except Exception:
        logger.exception("Failed to notify root admin about a new chat")
        return False
    return True


def format_status_fields(fields: Sequence[object], title: str) -> str:
    lines = [title]
    grouped: dict[str, list[str]] = {}
    for field in fields:
        entry_title = getattr(field, "entry_title", None)
        group_title = str(field.block_title)
        if entry_title:
            group_title = f"{group_title} — {entry_title}"
        grouped.setdefault(group_title, []).append(str(field.field_label))
    for group_title, labels in grouped.items():
        lines.extend(["", html.escape(group_title)])
        lines.extend(f"- {html.escape(label)}" for label in labels)
    return "\n".join(lines)
