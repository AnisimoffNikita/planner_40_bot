from __future__ import annotations

import html

from telegram import Update
from telegram.ext import ContextTypes

from meeting_bot.card_service import DomainError, StaleChange
from meeting_bot.command_catalog import sync_user_command_menu
from meeting_bot.handlers.common import require_access
from meeting_bot.handlers.update_wizard import wizard_keyboard


def services(context: ContextTypes.DEFAULT_TYPE) -> object:
    return context.application.bot_data["services"]


async def pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    access = await require_access(update, context, editable=True, command="pending")
    if access is None:
        return
    _, action, raw_id = query.data.split(":", 2)
    pending_id = int(raw_id)
    app = services(context)
    try:
        item = await app.cards.resolve_pending(
            pending_id, access.user.telegram_user_id, approve=action == "a"
        )
    except StaleChange as exc:
        await app.update_wizard.resume_after_pending(
            access.user.telegram_user_id, access.chat.chat_id, pending_id, "expired"
        )
        await query.edit_message_text(f"⌛ Изменение устарело.\n\n{html.escape(str(exc))}")
        return
    except DomainError as exc:
        await app.update_wizard.resume_after_pending(
            access.user.telegram_user_id, access.chat.chat_id, pending_id, "expired"
        )
        await query.edit_message_text(html.escape(str(exc)))
        return
    labels = {
        "approved": "✅ Изменение применено.",
        "cancelled": "❌ Изменение отменено.",
        "expired": "⌛ Изменение устарело.",
        "pending": "Изменение ожидает подтверждения.",
    }
    status_text = labels.get(item.status, item.status)
    render = await app.update_wizard.resume_after_pending(
        access.user.telegram_user_id,
        access.chat.chat_id,
        item.id,
        item.status,
    )
    if render is not None:
        await query.edit_message_text(
            f"{html.escape(status_text)}\n\n{html.escape(render.text)}",
            reply_markup=wizard_keyboard(render),
        )
        return
    await query.edit_message_text(f"{html.escape(item.preview_text)}\n\n{html.escape(status_text)}")


async def user_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    access = await require_access(update, context, command="users")
    if access is None:
        return
    if access.user.role != "admin":
        await query.answer("Только root-admin.", show_alert=True)
        return
    _, action, raw_id = query.data.split(":", 2)
    target_id = int(raw_id)
    if action == "r":
        user = await services(context).access.decide_user(
            access.user.telegram_user_id, target_id, status="rejected"
        )
        text = "Заявка отклонена."
    else:
        role = "viewer" if action == "v" else "editor"
        user = await services(context).access.decide_user(
            access.user.telegram_user_id,
            target_id,
            status="approved",
            role=role,
        )
        text = f"Доступ одобрен: {role}."
    await sync_user_command_menu(context.bot, user)
    await query.edit_message_text(
        f"Пользователь <code>{target_id}</code>\nРешение: {html.escape(user.status)}"
    )
    try:
        await context.bot.send_message(user.telegram_user_id, text)
    except Exception:
        pass


async def chat_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    access = await require_access(update, context, command="chats")
    if access is None:
        return
    if access.user.role != "admin":
        await query.answer("Только root-admin.", show_alert=True)
        return
    _, action, raw_id = query.data.split(":", 2)
    chat_id = int(raw_id)
    status = "approved" if action == "a" else "rejected"
    chat = await services(context).access.decide_chat(
        access.user.telegram_user_id,
        chat_id,
        status=status,
    )
    await query.edit_message_text(
        f"Чат <code>{chat.chat_id}</code>\nРешение: {html.escape(chat.status)}"
    )
