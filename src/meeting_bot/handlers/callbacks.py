from __future__ import annotations

import html

from telegram import Update
from telegram.ext import ContextTypes

from meeting_bot.card_service import StaleChange
from meeting_bot.handlers.common import require_access


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
    try:
        item = await services(context).cards.resolve_pending(
            int(raw_id), access.user.telegram_user_id, approve=action == "a"
        )
    except StaleChange as exc:
        await query.edit_message_text(f"⌛ Изменение устарело.\n\n{html.escape(str(exc))}")
        return
    labels = {
        "approved": "✅ Изменение применено.",
        "cancelled": "❌ Изменение отменено.",
        "expired": "⌛ Изменение устарело.",
        "pending": "Изменение ожидает подтверждения.",
    }
    await query.edit_message_text(
        f"{html.escape(item.preview_text)}\n\n{labels.get(item.status, item.status)}"
    )


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
    await query.edit_message_text(
        f"Пользователь <code>{target_id}</code>\nРешение: {html.escape(user.status)}"
    )
    try:
        await context.bot.send_message(user.telegram_user_id, text)
    except Exception:
        pass
