from __future__ import annotations

import html
import json
import re

from telegram import Update
from telegram.ext import ContextTypes

from meeting_bot.card_service import DomainError
from meeting_bot.handlers import update_wizard
from meeting_bot.handlers.common import (
    notify_root_admin_about_new_chat,
    pending_keyboard,
    require_access,
)
from meeting_bot.llm_client import LlmUnavailable

GROUP_MENTION_RE = re.compile(r"^@(?P<username>[A-Za-z0-9_]+)(?P<tail>$|[\s,:]+)")
ACTIVE_BOT_CHAT_STATUSES = {"member", "administrator"}


def services(context: ContextTypes.DEFAULT_TYPE) -> object:
    return context.application.bot_data["services"]


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or message.text is None or chat is None:
        return

    if chat.type in {"group", "supergroup"}:
        group_text = await tagged_group_text(context, message.text)
        if group_text is None:
            return
        access = await require_access(update, context, allow_group_read_only=True)
        if access is None:
            return
        if not group_text:
            await message.reply_text("Напиши вопрос после @BOTNAME.")
            return
        await process_natural_text(update, context, access, group_text)
        return

    access = await require_access(update, context)
    if access is None:
        return
    if await update_wizard.try_handle_text_input(update, context, access, message.text):
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
    if await update_wizard.try_handle_text_input(update, context, access, text):
        return
    await process_natural_text(update, context, access, text)


async def bot_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member_update = update.my_chat_member
    if member_update is None:
        return
    chat = member_update.chat
    if chat.type not in {"group", "supergroup"}:
        return
    old_status = getattr(member_update.old_chat_member, "status", None)
    new_status = getattr(member_update.new_chat_member, "status", None)
    if new_status not in ACTIVE_BOT_CHAT_STATUSES or old_status in ACTIVE_BOT_CHAT_STATUSES:
        return
    actor = getattr(member_update, "from_user", None)
    observation = await services(context).access.observe_group_chat(
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat.title,
        actor_user_id=getattr(actor, "id", None),
    )
    if observation.is_new_chat and observation.chat.status == "pending":
        await notify_root_admin_about_new_chat(
            context,
            chat_id=observation.chat.chat_id,
            chat_type=observation.chat.chat_type,
            chat_title=observation.chat.title,
        )


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
            role=access.llm_role,
            clarification_context=clarification_context,
        )
    except LlmUnavailable as exc:
        await message.reply_text(
            f"{html.escape(str(exc))} Команды /status, /summary и /update продолжают работать."
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
        try:
            pending = await app.cards.create_pending(
                user_id=access.user.telegram_user_id,
                chat_id=access.chat.chat_id,
                operations=result.patches,
            )
        except DomainError as exc:
            await message.reply_text(
                "Я понял запрос как изменение, но не смог безопасно подготовить preview: "
                f"{html.escape(str(exc))}\n"
                "Уточни блок, поле или конкретный элемент."
            )
            return
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


async def tagged_group_text(context: ContextTypes.DEFAULT_TYPE, text: str) -> str | None:
    username = await bot_username(context)
    if username is None:
        return None
    match = GROUP_MENTION_RE.match(text.strip())
    if match is None or match.group("username").casefold() != username.casefold():
        return None
    return text.strip()[match.end() :].lstrip(" \t\r\n,:")


async def bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    username = getattr(context.bot, "username", None)
    if username:
        return str(username).lstrip("@")
    get_me = getattr(context.bot, "get_me", None)
    if get_me is None:
        return None
    me = await get_me()
    username = getattr(me, "username", None)
    if not username:
        return None
    return str(username).lstrip("@")
