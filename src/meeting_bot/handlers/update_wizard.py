from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from meeting_bot.handlers.common import pending_keyboard, require_access
from meeting_bot.update_wizard import WizardOutcome, WizardRender


def services(context: ContextTypes.DEFAULT_TYPE) -> object:
    return context.application.bot_data["services"]


def wizard_keyboard(render: WizardRender) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(button.label, callback_data=button.callback_data)
                for button in row
            ]
            for row in render.rows
        ]
    )


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, editable=True, command="update")
    message = update.effective_message
    if access is None or message is None:
        return
    app = services(context)
    render = await app.update_wizard.start(access.user.telegram_user_id, access.chat.chat_id)
    sent = await message.reply_text(html.escape(render.text), reply_markup=wizard_keyboard(render))
    await app.update_wizard.set_message_id(access.user.telegram_user_id, sent.message_id)


async def wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    access = await require_access(update, context, editable=True, command="update")
    if access is None:
        return
    outcome = await services(context).update_wizard.handle_callback(
        access.user.telegram_user_id,
        access.chat.chat_id,
        query.data,
    )
    await _present_callback_outcome(update, context, outcome)


async def try_handle_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    access: object,
    text: str,
) -> bool:
    outcome = await services(context).update_wizard.handle_text(
        access.user.telegram_user_id,
        access.chat.chat_id,
        text,
    )
    if outcome is None:
        return False
    message = update.effective_message
    if message is None:
        return True
    if outcome.render is not None:
        await message.reply_text(
            html.escape(outcome.render.text),
            reply_markup=wizard_keyboard(outcome.render),
        )
    elif outcome.pending is not None:
        sent = await message.reply_text(
            html.escape(outcome.pending.preview_text),
            reply_markup=pending_keyboard(outcome.pending.id),
        )
        await services(context).set_pending_message_id(outcome.pending.id, sent.message_id)
    elif outcome.message:
        await message.reply_text(html.escape(outcome.message))
    return True


async def _present_callback_outcome(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    outcome: WizardOutcome,
) -> None:
    query = update.callback_query
    if query is None:
        return
    if outcome.render is not None:
        await query.edit_message_text(
            html.escape(outcome.render.text),
            reply_markup=wizard_keyboard(outcome.render),
        )
        return
    if outcome.pending is not None:
        await query.edit_message_text(
            html.escape(outcome.pending.preview_text),
            reply_markup=pending_keyboard(outcome.pending.id),
        )
        if query.message is not None:
            await services(context).set_pending_message_id(
                outcome.pending.id, query.message.message_id
            )
        return
    if outcome.message:
        await query.edit_message_text(html.escape(outcome.message))
