from __future__ import annotations

import html
import json

from telegram import Update
from telegram.ext import ContextTypes

from meeting_bot.handlers.common import pending_keyboard, require_access
from meeting_bot.llm_client import LlmUnavailable


def services(context: ContextTypes.DEFAULT_TYPE) -> object:
    return context.application.bot_data["services"]


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context)
    message = update.effective_message
    if access is None or message is None or message.text is None:
        return
    if access.chat.chat_type != "private":
        await message.reply_text("В группе доступны только /status, /summary и /help.")
        return
    await process_natural_text(update, context, access, message.text)


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context)
    message = update.effective_message
    if access is None or message is None or message.voice is None:
        return
    if access.chat.chat_type != "private":
        await message.reply_text("Голосовые сообщения обрабатываются только в личном чате.")
        return
    if not access.can_use_llm:
        return
    telegram_file = await context.bot.get_file(message.voice.file_id)
    try:
        text = await services(context).voice.transcribe_telegram_voice(message.voice, telegram_file)
    except (ValueError, LlmUnavailable) as exc:
        await message.reply_text(html.escape(str(exc)))
        return
    shown = text if len(text) <= 500 else text[:497] + "..."
    await message.reply_text(f"Я распознал: <i>{html.escape(shown)}</i>")
    await process_natural_text(update, context, access, text)


async def process_natural_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    access: object,
    text: str,
) -> None:
    message = update.effective_message
    app = services(context)
    if not access.can_use_llm:
        return
    card = await app.cards.get_or_create_current()
    clarification = await app.clarifications.consume(
        access.user.telegram_user_id, access.chat.chat_id
    )
    clarification_context = None
    if clarification is not None:
        clarification_context = json.dumps(
            {
                "original_text": clarification.original_text,
                "question": clarification.question,
                "partial_patches": json.loads(clarification.context_json),
            },
            ensure_ascii=False,
        )
    try:
        result = await app.llm.parse(
            text=text,
            schema=app.loaded_schema.schema,
            card_data=app.cards.card_data(card),
            role=access.user.role,
            clarification_context=clarification_context,
        )
    except LlmUnavailable as exc:
        await message.reply_text(
            f"{html.escape(str(exc))} Команды /status, /summary и /set продолжают работать."
        )
        return
    if result.needs_clarification:
        question = result.clarification_question or "Уточни, пожалуйста, запрос."
        await app.clarifications.save(
            access.user.telegram_user_id,
            access.chat.chat_id,
            text,
            question,
            result.patches,
        )
        await message.reply_text(html.escape(question))
        return
    if result.intent == "propose_update":
        if not access.can_edit:
            await message.reply_text("У тебя доступ read-only; изменить карточку нельзя.")
            return
        pending = await app.cards.create_pending(
            user_id=access.user.telegram_user_id,
            chat_id=access.chat.chat_id,
            operations=result.patches,
        )
        sent = await message.reply_text(
            html.escape(pending.preview_text), reply_markup=pending_keyboard(pending.id)
        )
        await app.set_pending_message_id(pending.id, sent.message_id)
    elif result.intent == "show_status":
        card_schema, fallback = await app.cards.schema_for_card(card)
        path = app.pdf.build(
            card,
            card_schema,
            app.cards.status_blocks(card, card_schema),
            schema_fallback=fallback,
        )
        with path.open("rb") as document:
            await message.reply_document(document, filename=path.name)
    elif result.intent == "show_history":
        cards = await app.cards.history()
        await message.reply_text(
            "\n".join(["Последние недели:"] + [f"• {item.week_start_date}" for item in cards])
        )
    elif result.answer:
        await message.reply_text(html.escape(result.answer))
    else:
        await message.reply_text("Я не уверен, что именно нужно. Уточни запрос.")
