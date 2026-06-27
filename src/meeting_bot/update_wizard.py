from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import cast

from sqlalchemy import delete

from meeting_bot.card_service import CardService, DomainError
from meeting_bot.domain import PatchOperation
from meeting_bot.models import PendingChange, UpdateWizardSession
from meeting_bot.schema import BlockSpec, FieldSpec
from meeting_bot.storage import Database

PAGE_SIZE = 6
SESSION_TTL = timedelta(minutes=30)
GENERIC_INPUT_HINTS = {
    "строка",
    "текст",
    "любая строка",
    "любое значение",
    "контакт тг",
    "телеграмм контакт",
    "имя фамилия",
    "название",
    "название книги",
    "мм:сс",
}


@dataclass(frozen=True)
class WizardButton:
    label: str
    callback_data: str


@dataclass(frozen=True)
class WizardOption:
    key: str
    label: str
    action: str
    payload: dict[str, object]


@dataclass(frozen=True)
class RawOption:
    label: str
    action: str
    payload: dict[str, object]


@dataclass(frozen=True)
class WizardRender:
    text: str
    rows: list[list[WizardButton]]
    context: dict[str, object]
    options: list[WizardOption]
    waiting_for_text: bool = False


@dataclass(frozen=True)
class WizardOutcome:
    render: WizardRender | None = None
    pending: PendingChange | None = None
    message: str | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class SessionLookup:
    session: UpdateWizardSession | None
    expired: bool = False


class UpdateWizardService:
    def __init__(self, database: Database, cards: CardService) -> None:
        self.database = database
        self.cards = cards

    async def start(self, user_id: int, chat_id: int) -> WizardRender:
        context: dict[str, object] = {"state": "blocks", "page": 0}
        return await self._render_and_save(user_id, chat_id, context)

    async def set_message_id(self, user_id: int, message_id: int) -> None:
        async with self.database.session() as session, session.begin():
            wizard = await session.get(UpdateWizardSession, user_id)
            if wizard is not None:
                wizard.message_id = message_id

    async def handle_callback(self, user_id: int, chat_id: int, data: str) -> WizardOutcome:
        lookup = await self._load_session(user_id, chat_id)
        if lookup.session is None:
            return WizardOutcome(message=self._missing_session_message(lookup.expired))
        context = self._decode_context(lookup.session.context_json)
        if self._state(context) == "pending_confirmation":
            return WizardOutcome(message="Сначала примени или отмени предложенное изменение.")
        if data == "uw:act:cancel":
            await self._delete_session(user_id)
            return WizardOutcome(message="Обновление отменено.", cancelled=True)
        if data == "uw:act:back":
            back = self._back_context(context)
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, back))
        if data in {"uw:act:prev", "uw:act:next"}:
            context["page"] = max(
                0,
                self._int_context(context, "page") + (-1 if data == "uw:act:prev" else 1),
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, context))
        if not data.startswith("uw:opt:"):
            return WizardOutcome(message="Кнопка устарела. Запусти /update заново.")

        option = self._option_by_key(lookup.session.options_json, data.removeprefix("uw:opt:"))
        if option is None:
            return WizardOutcome(message="Кнопка устарела. Запусти /update заново.")
        return await self._handle_option(user_id, chat_id, context, option)

    async def handle_text(self, user_id: int, chat_id: int, text: str) -> WizardOutcome | None:
        lookup = await self._load_session(user_id, chat_id)
        if lookup.session is None:
            if lookup.expired:
                return WizardOutcome(message=self._missing_session_message(expired=True))
            return None
        context = self._decode_context(lookup.session.context_json)
        if self._state(context) == "pending_confirmation":
            return WizardOutcome(message="Сначала примени или отмени предложенное изменение.")
        if self._state(context) != "text_input":
            return WizardOutcome(message="Выбери кнопку в интерфейсе обновления или нажми Отмена.")
        value = text.strip()
        if not value:
            return WizardOutcome(
                message="Пустое значение записать нельзя. Введи текст или нажми Отмена."
            )
        purpose = self._str_context(context, "purpose")
        if purpose == "add_entry":
            pending = await self._create_pending(
                user_id,
                chat_id,
                [
                    PatchOperation(
                        op="add_entry",
                        block_id=self._str_context(context, "block_id"),
                        value=value,
                        human_label=value,
                    )
                ],
            )
            await self._save_pending_confirmation(
                user_id,
                chat_id,
                pending.id,
                approved_context=self._add_entry_approved_context(context, pending),
                cancelled_context=self._multiple_entries_resume_context(context),
            )
            return WizardOutcome(pending=pending)
        if purpose == "set_field":
            pending = await self._create_pending(
                user_id,
                chat_id,
                [
                    PatchOperation(
                        op="set_field",
                        block_id=self._str_context(context, "block_id"),
                        entry_id=self._optional_str_context(context, "entry_id"),
                        field_id=self._str_context(context, "field_id"),
                        value=value,
                        human_label=await self._operation_label(context),
                    )
                ],
            )
            resume_context = self._field_list_resume_context(context)
            await self._save_pending_confirmation(
                user_id,
                chat_id,
                pending.id,
                approved_context=resume_context,
                cancelled_context=resume_context,
            )
            return WizardOutcome(pending=pending)
        raise DomainError("Неизвестное состояние ввода.")

    async def cleanup(self) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(
                delete(UpdateWizardSession).where(
                    UpdateWizardSession.expires_at < datetime.now(UTC).replace(tzinfo=None)
                )
            )

    async def resume_after_pending(
        self, user_id: int, chat_id: int, pending_id: int, status: str
    ) -> WizardRender | None:
        lookup = await self._load_session(user_id, chat_id)
        if lookup.session is None:
            return None
        context = self._decode_context(lookup.session.context_json)
        if self._state(context) != "pending_confirmation":
            return None
        if self._int_context(context, "pending_id") != pending_id:
            return None
        if status not in {"approved", "cancelled"}:
            await self._delete_session(user_id)
            return None
        key = "resume_on_approved" if status == "approved" else "resume_on_cancelled"
        resume_context = self._optional_dict_context(context, key)
        if resume_context is None:
            await self._delete_session(user_id)
            return None
        return await self._render_and_save(user_id, chat_id, resume_context)

    async def _handle_option(
        self,
        user_id: int,
        chat_id: int,
        context: dict[str, object],
        option: WizardOption,
    ) -> WizardOutcome:
        payload = option.payload
        action = option.action
        if action == "select_block":
            block_id = self._str_payload(payload, "block_id")
            block = self.cards.loaded_schema.schema.block_map[block_id]
            state = "multiple_entries" if block.is_multiple else "singleton_actions"
            next_context = self._with_back(
                {"state": state, "block_id": block_id, "page": 0}, context
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "edit_singleton":
            next_context = self._with_back(
                {
                    "state": "field_list",
                    "block_id": self._str_payload(payload, "block_id"),
                    "entry_id": None,
                    "page": 0,
                },
                context,
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "clear_block":
            block_id = self._str_payload(payload, "block_id")
            block = self.cards.loaded_schema.schema.block_map[block_id]
            resume_context = copy.deepcopy(context)
            pending = await self._create_pending(
                user_id,
                chat_id,
                [
                    PatchOperation(
                        op="clear_block",
                        block_id=block_id,
                        human_label=block.title,
                    )
                ],
            )
            await self._save_pending_confirmation(
                user_id,
                chat_id,
                pending.id,
                approved_context=resume_context,
                cancelled_context=resume_context,
            )
            return WizardOutcome(pending=pending)
        if action == "add_entry":
            block_id = self._str_payload(payload, "block_id")
            block = self.cards.loaded_schema.schema.block_map[block_id]
            next_context = self._with_back(
                {
                    "state": "text_input",
                    "purpose": "add_entry",
                    "block_id": block_id,
                    "prompt": f"Напиши название для «{block.title}».",
                },
                context,
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "select_entry":
            next_context = self._with_back(
                {
                    "state": "multiple_entry_actions",
                    "block_id": self._str_payload(payload, "block_id"),
                    "entry_id": self._str_payload(payload, "entry_id"),
                    "entry_title": self._str_payload(payload, "entry_title"),
                    "page": 0,
                },
                context,
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "edit_entry":
            next_context = self._with_back(
                {
                    "state": "field_list",
                    "block_id": self._str_payload(payload, "block_id"),
                    "entry_id": self._str_payload(payload, "entry_id"),
                    "entry_title": self._str_payload(payload, "entry_title"),
                    "page": 0,
                },
                context,
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "delete_entry":
            resume_context = self._multiple_entries_resume_context(context)
            pending = await self._create_pending(
                user_id,
                chat_id,
                [
                    PatchOperation(
                        op="delete_entry",
                        block_id=self._str_payload(payload, "block_id"),
                        entry_id=self._str_payload(payload, "entry_id"),
                        human_label=self._str_payload(payload, "entry_title"),
                    )
                ],
            )
            await self._save_pending_confirmation(
                user_id,
                chat_id,
                pending.id,
                approved_context=resume_context,
                cancelled_context=resume_context,
            )
            return WizardOutcome(pending=pending)
        if action == "select_field":
            next_context = self._with_back(
                {
                    "state": "value_list",
                    "block_id": self._str_payload(payload, "block_id"),
                    "entry_id": payload.get("entry_id"),
                    "entry_title": payload.get("entry_title"),
                    "field_id": self._str_payload(payload, "field_id"),
                    "page": 0,
                },
                context,
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "select_value":
            pending = await self._create_pending(
                user_id,
                chat_id,
                [
                    PatchOperation(
                        op="set_field",
                        block_id=self._str_payload(payload, "block_id"),
                        entry_id=self._optional_str_payload(payload, "entry_id"),
                        field_id=self._str_payload(payload, "field_id"),
                        value=self._str_payload(payload, "value"),
                        human_label=await self._operation_label_from_payload(payload),
                    )
                ],
            )
            resume_context = self._field_list_resume_context(context)
            await self._save_pending_confirmation(
                user_id,
                chat_id,
                pending.id,
                approved_context=resume_context,
                cancelled_context=resume_context,
            )
            return WizardOutcome(pending=pending)
        if action == "manual_value":
            field = self._field_from_payload(payload)
            next_context = self._with_back(
                {
                    "state": "text_input",
                    "purpose": "set_field",
                    "block_id": self._str_payload(payload, "block_id"),
                    "entry_id": payload.get("entry_id"),
                    "entry_title": payload.get("entry_title"),
                    "field_id": self._str_payload(payload, "field_id"),
                    "prompt": f"Введи значение для «{field.label}».",
                },
                context,
            )
            return WizardOutcome(render=await self._render_and_save(user_id, chat_id, next_context))
        if action == "clear_field":
            pending = await self._create_pending(
                user_id,
                chat_id,
                [
                    PatchOperation(
                        op="clear_field",
                        block_id=self._str_payload(payload, "block_id"),
                        entry_id=self._optional_str_payload(payload, "entry_id"),
                        field_id=self._str_payload(payload, "field_id"),
                        human_label=await self._operation_label_from_payload(payload),
                    )
                ],
            )
            resume_context = self._field_list_resume_context(context)
            await self._save_pending_confirmation(
                user_id,
                chat_id,
                pending.id,
                approved_context=resume_context,
                cancelled_context=resume_context,
            )
            return WizardOutcome(pending=pending)
        raise DomainError("Неизвестное действие интерфейса обновления.")

    async def _render_and_save(
        self, user_id: int, chat_id: int, context: dict[str, object]
    ) -> WizardRender:
        render = await self._render(context)
        await self._save_session(user_id, chat_id, render)
        return render

    async def _render(self, context: dict[str, object]) -> WizardRender:
        state = self._state(context)
        if state == "blocks":
            return self._render_blocks(context)
        if state == "singleton_actions":
            return self._render_singleton_actions(context)
        if state == "multiple_entries":
            return await self._render_multiple_entries(context)
        if state == "multiple_entry_actions":
            return self._render_multiple_entry_actions(context)
        if state == "field_list":
            return self._render_field_list(context)
        if state == "value_list":
            return self._render_value_list(context)
        if state == "text_input":
            return self._render_text_input(context)
        raise DomainError("Неизвестное состояние интерфейса обновления.")

    def _render_blocks(self, context: dict[str, object]) -> WizardRender:
        raw = [
            RawOption(block.title, "select_block", {"block_id": block.id})
            for block in self.cards.loaded_schema.schema.blocks
        ]
        page = self._int_context(context, "page")
        options, page, total_pages = self._page_options(raw, page)
        context = {**context, "page": page}
        text = f"Что изменить?\nСтраница {page + 1}/{total_pages}"
        rows = self._option_grid(options)
        self._add_pagination(rows, page, total_pages)
        rows.append([WizardButton("Отмена", "uw:act:cancel")])
        return WizardRender(text=text, rows=rows, context=context, options=options)

    def _render_singleton_actions(self, context: dict[str, object]) -> WizardRender:
        block = self._block_from_context(context)
        options = [
            WizardOption("o0", "Изменить", "edit_singleton", {"block_id": block.id}),
            WizardOption("o1", "Очистить", "clear_block", {"block_id": block.id}),
        ]
        rows = self._option_grid(options)
        self._add_back_cancel(rows)
        return WizardRender(
            text=f"Блок: {block.title}\nЧто сделать?",
            rows=rows,
            context=context,
            options=options,
        )

    async def _render_multiple_entries(self, context: dict[str, object]) -> WizardRender:
        block = self._block_from_context(context)
        entries = await self._entries(block.id)
        raw = [
            RawOption(
                self._entry_title(entry),
                "select_entry",
                {
                    "block_id": block.id,
                    "entry_id": str(entry.get("entry_id", "")),
                    "entry_title": self._entry_title(entry),
                },
            )
            for entry in entries
        ]
        page = self._int_context(context, "page")
        entry_options, page, total_pages = self._page_options(raw, page)
        context = {**context, "page": page}
        add_option = WizardOption("o0", "Добавить новый", "add_entry", {"block_id": block.id})
        options = [add_option] + [
            WizardOption(f"o{index + 1}", option.label, option.action, option.payload)
            for index, option in enumerate(entry_options)
        ]
        text = f"Блок: {block.title}\n"
        text += "Выбери добавленный элемент или добавь новый."
        if entries:
            text += f"\nСтраница {page + 1}/{total_pages}"
        else:
            text += "\nПока ничего не добавлено."
        rows = [[WizardButton(add_option.label, f"uw:opt:{add_option.key}")]]
        rows.extend(self._option_grid(options[1:]))
        self._add_pagination(rows, page, total_pages)
        self._add_back_cancel(rows)
        return WizardRender(text=text, rows=rows, context=context, options=options)

    def _render_multiple_entry_actions(self, context: dict[str, object]) -> WizardRender:
        block = self._block_from_context(context)
        entry_id = self._str_context(context, "entry_id")
        entry_title = self._str_context(context, "entry_title")
        options = [
            WizardOption(
                "o0",
                "Редактировать",
                "edit_entry",
                {"block_id": block.id, "entry_id": entry_id, "entry_title": entry_title},
            ),
            WizardOption(
                "o1",
                "Удалить",
                "delete_entry",
                {"block_id": block.id, "entry_id": entry_id, "entry_title": entry_title},
            ),
        ]
        rows = self._option_grid(options)
        self._add_back_cancel(rows)
        return WizardRender(
            text=f"{block.title} / {entry_title}\nЧто сделать?",
            rows=rows,
            context=context,
            options=options,
        )

    def _render_field_list(self, context: dict[str, object]) -> WizardRender:
        block = self._block_from_context(context)
        entry_id = self._optional_str_context(context, "entry_id")
        entry_title = self._optional_str_context(context, "entry_title")
        raw = [
            RawOption(
                field.label,
                "select_field",
                {
                    "block_id": block.id,
                    "entry_id": entry_id,
                    "entry_title": entry_title,
                    "field_id": field_id,
                },
            )
            for field_id, field in block.fields.items()
        ]
        page = self._int_context(context, "page")
        options, page, total_pages = self._page_options(raw, page)
        context = {**context, "page": page}
        title = block.title if entry_title is None else f"{block.title} / {entry_title}"
        rows = self._option_grid(options)
        self._add_pagination(rows, page, total_pages)
        self._add_back_cancel(rows)
        return WizardRender(
            text=f"{title}\nКакое поле изменить?\nСтраница {page + 1}/{total_pages}",
            rows=rows,
            context=context,
            options=options,
        )

    def _render_value_list(self, context: dict[str, object]) -> WizardRender:
        block = self._block_from_context(context)
        field_id = self._str_context(context, "field_id")
        field = block.fields[field_id]
        entry_id = self._optional_str_context(context, "entry_id")
        entry_title = self._optional_str_context(context, "entry_title")
        fixed_values, has_manual = self._value_choices(field)
        raw = [
            RawOption(
                value,
                "select_value",
                {
                    "block_id": block.id,
                    "entry_id": entry_id,
                    "entry_title": entry_title,
                    "field_id": field_id,
                    "value": value,
                },
            )
            for value in fixed_values
        ]
        page = self._int_context(context, "page")
        options, page, total_pages = self._page_options(raw, page)
        context = {**context, "page": page}
        next_index = len(options)
        if has_manual:
            options.append(
                WizardOption(
                    f"o{next_index}",
                    "Ввести вручную",
                    "manual_value",
                    {
                        "block_id": block.id,
                        "entry_id": entry_id,
                        "entry_title": entry_title,
                        "field_id": field_id,
                    },
                )
            )
            next_index += 1
        options.append(
            WizardOption(
                f"o{next_index}",
                "Очистить поле",
                "clear_field",
                {
                    "block_id": block.id,
                    "entry_id": entry_id,
                    "entry_title": entry_title,
                    "field_id": field_id,
                },
            )
        )
        title = block.title if entry_title is None else f"{block.title} / {entry_title}"
        rows = self._option_grid(options[: len(options) - (2 if has_manual else 1)])
        self._add_pagination(rows, page, total_pages)
        tail = options[len(options) - (2 if has_manual else 1) :]
        for option in tail:
            rows.append([WizardButton(option.label, f"uw:opt:{option.key}")])
        self._add_back_cancel(rows)
        return WizardRender(
            text=f"{title}\nПоле: {field.label}\nВыбери значение или введи вручную.",
            rows=rows,
            context=context,
            options=options,
        )

    def _render_text_input(self, context: dict[str, object]) -> WizardRender:
        rows: list[list[WizardButton]] = []
        self._add_back_cancel(rows)
        return WizardRender(
            text=self._str_context(context, "prompt"),
            rows=rows,
            context=context,
            options=[],
            waiting_for_text=True,
        )

    def _page_options(
        self, raw: Sequence[RawOption], page: int
    ) -> tuple[list[WizardOption], int, int]:
        total_pages = max(1, ceil(len(raw) / PAGE_SIZE))
        page = min(max(page, 0), total_pages - 1)
        start = page * PAGE_SIZE
        visible = raw[start : start + PAGE_SIZE]
        return (
            [
                WizardOption(f"o{index}", item.label, item.action, item.payload)
                for index, item in enumerate(visible)
            ],
            page,
            total_pages,
        )

    def _option_grid(self, options: Sequence[WizardOption]) -> list[list[WizardButton]]:
        rows: list[list[WizardButton]] = []
        for start in range(0, len(options), 2):
            rows.append(
                [
                    WizardButton(option.label, f"uw:opt:{option.key}")
                    for option in options[start : start + 2]
                ]
            )
        return rows

    def _add_pagination(self, rows: list[list[WizardButton]], page: int, total_pages: int) -> None:
        if total_pages <= 1:
            return
        row: list[WizardButton] = []
        if page > 0:
            row.append(WizardButton("←", "uw:act:prev"))
        if page < total_pages - 1:
            row.append(WizardButton("→", "uw:act:next"))
        if row:
            rows.append(row)

    def _add_back_cancel(self, rows: list[list[WizardButton]]) -> None:
        rows.append(
            [
                WizardButton("Назад", "uw:act:back"),
                WizardButton("Отмена", "uw:act:cancel"),
            ]
        )

    async def _save_session(self, user_id: int, chat_id: int, render: WizardRender) -> None:
        await self._save_context(user_id, chat_id, render.context, render.options)

    async def _save_context(
        self,
        user_id: int,
        chat_id: int,
        context: dict[str, object],
        options: Sequence[WizardOption],
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        state = self._state(context)
        context_json = json.dumps(context, ensure_ascii=False)
        options_json = json.dumps(
            [
                {
                    "key": option.key,
                    "label": option.label,
                    "action": option.action,
                    "payload": option.payload,
                }
                for option in options
            ],
            ensure_ascii=False,
        )
        async with self.database.session() as session, session.begin():
            existing = await session.get(UpdateWizardSession, user_id)
            if existing is None:
                session.add(
                    UpdateWizardSession(
                        user_id=user_id,
                        chat_id=chat_id,
                        state=state,
                        context_json=context_json,
                        options_json=options_json,
                        message_id=None,
                        created_at=now,
                        expires_at=now + SESSION_TTL,
                    )
                )
            else:
                existing.chat_id = chat_id
                existing.state = state
                existing.context_json = context_json
                existing.options_json = options_json
                existing.created_at = now
                existing.expires_at = now + SESSION_TTL

    async def _save_pending_confirmation(
        self,
        user_id: int,
        chat_id: int,
        pending_id: int,
        *,
        approved_context: dict[str, object],
        cancelled_context: dict[str, object],
    ) -> None:
        context: dict[str, object] = {
            "state": "pending_confirmation",
            "pending_id": pending_id,
            "resume_on_approved": copy.deepcopy(approved_context),
            "resume_on_cancelled": copy.deepcopy(cancelled_context),
        }
        await self._save_context(user_id, chat_id, context, [])

    async def _load_session(self, user_id: int, chat_id: int) -> SessionLookup:
        async with self.database.session() as session, session.begin():
            existing = await session.get(UpdateWizardSession, user_id)
            if existing is None:
                return SessionLookup(None)
            if existing.chat_id != chat_id:
                return SessionLookup(None)
            if existing.expires_at < datetime.now(UTC).replace(tzinfo=None):
                await session.delete(existing)
                return SessionLookup(None, expired=True)
            return SessionLookup(existing)

    async def _delete_session(self, user_id: int) -> None:
        async with self.database.session() as session, session.begin():
            existing = await session.get(UpdateWizardSession, user_id)
            if existing is not None:
                await session.delete(existing)

    async def _create_pending(
        self, user_id: int, chat_id: int, operations: list[PatchOperation]
    ) -> PendingChange:
        return await self.cards.create_pending(
            user_id=user_id,
            chat_id=chat_id,
            operations=operations,
        )

    def _field_list_resume_context(self, context: dict[str, object]) -> dict[str, object]:
        existing = self._ancestor_context(context, "field_list")
        if existing is not None:
            return existing
        resume: dict[str, object] = {
            "state": "field_list",
            "block_id": self._str_context(context, "block_id"),
            "entry_id": self._optional_str_context(context, "entry_id"),
            "page": 0,
        }
        entry_title = self._optional_str_context(context, "entry_title")
        if entry_title is not None:
            resume["entry_title"] = entry_title
        return resume

    def _multiple_entries_resume_context(self, context: dict[str, object]) -> dict[str, object]:
        existing = self._ancestor_context(context, "multiple_entries")
        if existing is not None:
            return existing
        return {
            "state": "multiple_entries",
            "block_id": self._str_context(context, "block_id"),
            "page": 0,
        }

    def _add_entry_approved_context(
        self, context: dict[str, object], pending: PendingChange
    ) -> dict[str, object]:
        operation = self._first_pending_operation(pending)
        block_id = self._str_context(context, "block_id")
        entry_id = operation.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            raise DomainError("Не удалось открыть новый элемент после подтверждения.")
        entry_title = operation.get("value") or operation.get("human_label") or "Без названия"
        if not isinstance(entry_title, str):
            entry_title = "Без названия"
        entries_context = self._multiple_entries_resume_context(context)
        entry_actions_context = self._with_back(
            {
                "state": "multiple_entry_actions",
                "block_id": block_id,
                "entry_id": entry_id,
                "entry_title": entry_title,
                "page": 0,
            },
            entries_context,
        )
        return self._with_back(
            {
                "state": "field_list",
                "block_id": block_id,
                "entry_id": entry_id,
                "entry_title": entry_title,
                "page": 0,
            },
            entry_actions_context,
        )

    def _ancestor_context(self, context: dict[str, object], state: str) -> dict[str, object] | None:
        cursor = copy.deepcopy(context)
        for _ in range(10):
            if cursor.get("state") == state:
                return cursor
            back = cursor.get("back")
            if not isinstance(back, dict):
                return None
            cursor = copy.deepcopy(cast(dict[str, object], back))
        return None

    def _first_pending_operation(self, pending: PendingChange) -> dict[str, object]:
        loaded = json.loads(pending.patch_json)
        if not isinstance(loaded, list) or not loaded or not isinstance(loaded[0], dict):
            raise DomainError("Предложение изменения повреждено.")
        return cast(dict[str, object], loaded[0])

    async def _entries(self, block_id: str) -> list[dict[str, object]]:
        card = await self.cards.get_or_create_current()
        data = self.cards.card_data(card)
        blocks = data.get("blocks", {})
        if not isinstance(blocks, dict):
            return []
        entries = blocks.get(block_id, [])
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    async def _entry_title_by_id(self, block_id: str, entry_id: str) -> str:
        for entry in await self._entries(block_id):
            if str(entry.get("entry_id", "")) == entry_id:
                return self._entry_title(entry)
        return "Без названия"

    def _entry_title(self, entry: dict[str, object]) -> str:
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        fields = entry.get("fields")
        if isinstance(fields, dict):
            for key in ("title", "topic", "name", "block_type"):
                value = fields.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in fields.values():
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return "Без названия"

    async def _operation_label(self, context: dict[str, object]) -> str:
        entry_id = self._optional_str_context(context, "entry_id")
        if entry_id is not None:
            entry_title = self._optional_str_context(context, "entry_title")
            if entry_title:
                return entry_title
            return await self._entry_title_by_id(self._str_context(context, "block_id"), entry_id)
        block = self._block_from_context(context)
        field_id = self._str_context(context, "field_id")
        return block.fields[field_id].label

    async def _operation_label_from_payload(self, payload: dict[str, object]) -> str:
        entry_id = self._optional_str_payload(payload, "entry_id")
        if entry_id is not None:
            entry_title = self._optional_str_payload(payload, "entry_title")
            if entry_title:
                return entry_title
            return await self._entry_title_by_id(self._str_payload(payload, "block_id"), entry_id)
        block = self.cards.loaded_schema.schema.block_map[self._str_payload(payload, "block_id")]
        return block.fields[self._str_payload(payload, "field_id")].label

    def _value_choices(self, field: FieldSpec) -> tuple[list[str], bool]:
        fixed: list[str] = []
        has_manual = False
        seen: set[str] = set()
        for value in field.allowed_values:
            if self._is_manual_hint(value):
                has_manual = True
                continue
            if value not in seen:
                fixed.append(value)
                seen.add(value)
        return fixed, has_manual or not fixed

    def _is_manual_hint(self, value: str) -> bool:
        stripped = value.strip()
        folded = stripped.casefold()
        return ("<" in stripped and ">" in stripped) or folded in GENERIC_INPUT_HINTS

    def _block_from_context(self, context: dict[str, object]) -> BlockSpec:
        return self.cards.loaded_schema.schema.block_map[self._str_context(context, "block_id")]

    def _field_from_payload(self, payload: dict[str, object]) -> FieldSpec:
        block = self.cards.loaded_schema.schema.block_map[self._str_payload(payload, "block_id")]
        return block.fields[self._str_payload(payload, "field_id")]

    def _back_context(self, context: dict[str, object]) -> dict[str, object]:
        back = context.get("back")
        if isinstance(back, dict):
            return copy.deepcopy(cast(dict[str, object], back))
        return {"state": "blocks", "page": 0}

    def _with_back(self, context: dict[str, object], back: dict[str, object]) -> dict[str, object]:
        next_context = copy.deepcopy(context)
        next_context["back"] = copy.deepcopy(back)
        return next_context

    def _decode_context(self, raw: str) -> dict[str, object]:
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise DomainError("Сессия обновления повреждена.")
        return cast(dict[str, object], loaded)

    def _option_by_key(self, raw: str, key: str) -> WizardOption | None:
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            return None
        for item in loaded:
            if not isinstance(item, dict) or item.get("key") != key:
                continue
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            return WizardOption(
                key=str(item.get("key", "")),
                label=str(item.get("label", "")),
                action=str(item.get("action", "")),
                payload=cast(dict[str, object], payload),
            )
        return None

    def _state(self, context: dict[str, object]) -> str:
        return self._str_context(context, "state")

    def _str_context(self, context: dict[str, object], key: str) -> str:
        value = context.get(key)
        if not isinstance(value, str) or not value:
            raise DomainError("Сессия обновления повреждена.")
        return value

    def _optional_str_context(self, context: dict[str, object], key: str) -> str | None:
        value = context.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise DomainError("Сессия обновления повреждена.")
        return value

    def _optional_dict_context(
        self, context: dict[str, object], key: str
    ) -> dict[str, object] | None:
        value = context.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise DomainError("Сессия обновления повреждена.")
        return copy.deepcopy(cast(dict[str, object], value))

    def _int_context(self, context: dict[str, object], key: str) -> int:
        value = context.get(key)
        if isinstance(value, int):
            return value
        return 0

    def _str_payload(self, payload: dict[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise DomainError("Кнопка обновления повреждена.")
        return value

    def _optional_str_payload(self, payload: dict[str, object], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise DomainError("Кнопка обновления повреждена.")
        return value

    def _missing_session_message(self, expired: bool) -> str:
        if expired:
            return "Сессия обновления устарела. Запусти /update заново."
        return "Интерфейс обновления уже закрыт. Запусти /update заново."
