from __future__ import annotations

import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from meeting_bot.domain import PatchOperation
from meeting_bot.handlers.common import (
    format_status_fields,
    pending_keyboard,
    require_access,
    send_long,
)

SET_PATTERN = re.compile(
    r"^(?P<block>[A-Za-z][\w-]*)(?:\[(?P<entry>[^\]]+)\])?\."
    r"(?P<field>[A-Za-z][\w-]*)\s+(?P<value>.+)$",
    re.DOTALL,
)
DELETE_PATTERN = re.compile(r"^(?P<block>[A-Za-z][\w-]*)\[(?P<entry>[^\]]+)\]$")
HISTORY_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<week>\d{2})$")


def services(context: ContextTypes.DEFAULT_TYPE) -> object:
    return context.application.bot_data["services"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, approved=False, command="start")
    if access is None:
        return
    if access.blocked:
        return
    message = update.effective_message
    if message is None:
        return
    if access.approved:
        await message.reply_text(
            f"Доступ активен: <b>{html.escape(access.user.role)}</b>. "
            "Используй /help, чтобы увидеть команды."
        )
    elif access.user.status == "pending":
        await message.reply_text("Ты пока не одобрен. Я отправил заявку администратору.")
    else:
        await message.reply_text("Доступ не одобрен. Обратитесь к администратору.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, command="help")
    if access is None:
        return
    base = (
        "<b>Команды</b>\n"
        "/whoami — роль и статус\n"
        "/status — PDF текущей карточки\n"
        "/summary — краткий статус\n"
        "/history [YYYY-WW] — архив\n"
        "/schema — версия и блоки"
    )
    if access.can_edit:
        base += (
            "\n\n<b>Изменения</b>\n"
            "/set block.field значение\n"
            "/set block[entry_id].field значение\n"
            "/add block название\n"
            "/delete block[entry_id]\n"
            "/pending — ожидающие подтверждения\n"
            "/cancel ID — отменить предложение"
        )
    if access.user.role == "admin":
        base += (
            "\n\n<b>Администрирование</b>\n"
            "/users, /approve ID viewer|editor, /reject ID\n"
            "/block_user ID, /unblock_user ID\n"
            "/block_chat ID, /unblock_chat ID, /audit [limit]"
        )
    await update.effective_message.reply_text(base)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, approved=False, command="whoami")
    if access is None:
        return
    await update.effective_message.reply_text(
        f"Роль: <b>{html.escape(access.user.role)}</b>\n"
        f"Статус: <b>{html.escape(access.user.status)}</b>\n"
        f"Чат: <b>{html.escape(access.chat.chat_type)}</b>"
        + (" (read-only)" if access.chat.read_only else "")
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_access(update, context, command="status") is None:
        return
    app = services(context)
    card = await app.cards.get_or_create_current()
    schema, fallback = await app.cards.schema_for_card(card)
    blocks = app.cards.status_blocks(card, schema)
    path = app.pdf.build(card, schema, blocks, schema_fallback=fallback)
    with path.open("rb") as document:
        await update.effective_message.reply_document(
            document=document, filename=path.name, caption=f"Статус недели {card.week_start_date}"
        )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_access(update, context, command="summary") is None:
        return
    app = services(context)
    card = await app.cards.get_or_create_current()
    schema, _ = await app.cards.schema_for_card(card)
    summary = app.cards.summary(app.cards.status_blocks(card, schema))
    text = (
        f"<b>Неделя {card.week_start_date}</b>\n"
        f"Готово: {summary['ready']} из {summary['total']}\n"
        f"Просрочено: {summary['overdue']}\n"
        f"Дедлайн сегодня: {summary['due_today']}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Открыть PDF", callback_data="s:pdf")],
            [
                InlineKeyboardButton("Что просрочено", callback_data="s:overdue"),
                InlineKeyboardButton("Что сегодня", callback_data="s:today"),
            ],
        ]
    )
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_access(update, context, command="history") is None:
        return
    app = services(context)
    if not context.args:
        cards = await app.cards.history()
        lines = ["<b>Последние карточки</b>"]
        lines.extend(
            f"• <code>{card.week_start_date}</code> · схема {html.escape(card.schema_version)}"
            for card in cards
        )
        await update.effective_message.reply_text("\n".join(lines))
        return
    match = HISTORY_PATTERN.fullmatch(context.args[0])
    if match is None:
        await update.effective_message.reply_text("Формат: /history YYYY-WW")
        return
    try:
        card = await app.cards.card_for_iso_week(int(match.group("year")), int(match.group("week")))
    except ValueError:
        card = None
    if card is None:
        await update.effective_message.reply_text("Карточка этой недели не найдена.")
        return
    schema, fallback = await app.cards.schema_for_card(card)
    blocks = app.cards.status_blocks(card, schema)
    path = app.pdf.build(card, schema, blocks, schema_fallback=fallback)
    with path.open("rb") as document:
        await update.effective_message.reply_document(document, filename=path.name)


async def schema_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_access(update, context, command="schema") is None:
        return
    loaded = services(context).loaded_schema
    lines = [
        f"<b>{html.escape(loaded.schema.title)}</b>",
        f"Версия: <code>{html.escape(loaded.schema.version)}</code>",
        f"Hash: <code>{loaded.schema_hash[:12]}</code>",
        "",
    ]
    lines.extend(
        f"• <code>{html.escape(block.id)}</code> — {html.escape(block.title)}"
        + (" (повторяемый)" if block.multiple else "")
        for block in loaded.schema.blocks
    )
    await send_long(update.effective_message, "\n".join(lines))


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, editable=True, command="set")
    if access is None:
        return
    raw = " ".join(context.args)
    match = SET_PATTERN.fullmatch(raw)
    if match is None:
        await update.effective_message.reply_text(
            "Формат: /set block.field значение или /set block[entry_id].field значение"
        )
        return
    operation = PatchOperation(
        op="set_field",
        block_id=match.group("block"),
        entry_id=match.group("entry"),
        field_id=match.group("field"),
        value=match.group("value"),
        human_label=match.group("field"),
    )
    pending = await services(context).cards.create_pending(
        user_id=access.user.telegram_user_id,
        chat_id=access.chat.chat_id,
        operations=[operation],
    )
    sent = await update.effective_message.reply_text(
        html.escape(pending.preview_text), reply_markup=pending_keyboard(pending.id)
    )
    await services(context).set_pending_message_id(pending.id, sent.message_id)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, editable=True, command="add")
    if access is None:
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Формат: /add block_id название")
        return
    block_id, title = context.args[0], " ".join(context.args[1:])
    pending = await services(context).cards.create_pending(
        user_id=access.user.telegram_user_id,
        chat_id=access.chat.chat_id,
        operations=[
            PatchOperation(
                op="add_entry",
                block_id=block_id,
                value=title,
                human_label=title,
            )
        ],
    )
    sent = await update.effective_message.reply_text(
        html.escape(pending.preview_text), reply_markup=pending_keyboard(pending.id)
    )
    await services(context).set_pending_message_id(pending.id, sent.message_id)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, editable=True, command="delete")
    if access is None:
        return
    raw = " ".join(context.args)
    match = DELETE_PATTERN.fullmatch(raw)
    if match is None:
        await update.effective_message.reply_text("Формат: /delete block[entry_id]")
        return
    pending = await services(context).cards.create_pending(
        user_id=access.user.telegram_user_id,
        chat_id=access.chat.chat_id,
        operations=[
            PatchOperation(
                op="delete_entry",
                block_id=match.group("block"),
                entry_id=match.group("entry"),
                human_label=match.group("entry"),
            )
        ],
    )
    sent = await update.effective_message.reply_text(
        html.escape(pending.preview_text), reply_markup=pending_keyboard(pending.id)
    )
    await services(context).set_pending_message_id(pending.id, sent.message_id)


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, editable=True, command="pending")
    if access is None:
        return
    pending = await services(context).cards.pending_for_user(access.user.telegram_user_id)
    if not pending:
        await update.effective_message.reply_text("Ожидающих подтверждения изменений нет.")
        return
    for item in pending:
        await update.effective_message.reply_text(
            f"<b>#{item.id}</b>\n{html.escape(item.preview_text)}",
            reply_markup=pending_keyboard(item.id),
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access = await require_access(update, context, editable=True, command="cancel")
    if access is None:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Формат: /cancel pending_change_id")
        return
    item = await services(context).cards.resolve_pending(
        int(context.args[0]), access.user.telegram_user_id, approve=False
    )
    await update.effective_message.reply_text(f"Изменение #{item.id}: {item.status}.")


async def summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if await require_access(update, context, command="summary") is None:
        return
    action = query.data or ""
    if action == "s:pdf":
        await status_command(update, context)
        return
    app = services(context)
    card = await app.cards.get_or_create_current()
    schema, _ = await app.cards.schema_for_card(card)
    summary = app.cards.summary(app.cards.status_blocks(card, schema))
    key = "overdue" if action == "s:overdue" else "due_today"
    title = "Просрочено" if key == "overdue" else "Нужно сегодня"
    fields = [field for field in summary["fields"] if field.evaluation.deadline_state == key]
    await query.message.reply_text(
        format_status_fields(fields, title) if fields else f"{title}: ничего."
    )
